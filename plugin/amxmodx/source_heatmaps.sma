/*
 * Source Heatmaps - GoldSrc / AMX Mod X collector (HLDM, TFC, DMC, Ricochet, ...)
 *
 * Same data as the SourceMod plugin: samples every living player and bot once per interval,
 * buckets positions in memory (32-unit cells; 64-unit cells for movement edges) and flushes them
 * to the MySQL staging table hm_ingest (the account needs column-level INSERT only).
 *
 * Positions are the player's feet (origin + hull mins), like Source's GetClientAbsOrigin.
 * Canonical classes: 0 none, 1 scout, 2 soldier, 3 pyro, 4 demoman, 5 heavy, 6 engineer,
 * 7 medic, 8 sniper, 9 spy, 10 civilian (TFC: hwguy = heavy, civilian = 10).
 *
 * Needs AMX Mod X 1.8.2+ with the MySQL module (loaded automatically by sqlx).
 * Settings: addons/amxmodx/configs/source_heatmaps.cfg (executed at map start).
 */
#include <amxmodx>
#include <amxmisc>
#include <fakemeta>
#include <sqlx>

#define PLUGIN  "Source Heatmaps (GoldSrc)"
#define VERSION "1.0.0"
#define AUTHOR  "Vio's Arcade"

#define KIND_GROUND 0
#define KIND_DEATH  1
#define KIND_EDGE   2
#define KIND_AIR    3
#define CELLSIZE    32.0
#define EDGESIZE    64.0
#define ROWS_PER_QUERY 100
#define TASK_SAMPLE 81000
#define TASK_FLUSH  81001
#define TASK_DEATH  81100

new g_cvEnable, g_cvInterval, g_cvFlush, g_cvBots, g_cvGame, g_cvHost, g_cvUser, g_cvPass, g_cvDb, g_cvMax
new Handle:g_tuple = Empty_Handle
new Trie:g_index
new Array:g_vals          // 10 cells per bucket: kind, team, cls, bot, x, y, z, x2, y2, z2
new Array:g_counts
new g_game[32], g_map[96], g_port
new bool:g_tfc, bool:g_teamGame
new Float:g_last[33][3], bool:g_hasLast[33]
new Float:g_deathPos[33][3], g_deathTeam[33], g_deathCls[33]
new bool:g_teamSent[16]
new g_errors, g_samples

// TFC pev_playerclass -> canonical (1 scout, 2 sniper, 3 soldier, 4 demo, 5 medic, 6 hwguy,
// 7 pyro, 8 spy, 9 engineer, 10 random, 11 civilian)
new const TFC_CLASS[12] = { 0, 1, 8, 2, 4, 7, 5, 3, 9, 6, 0, 10 }

public plugin_init()
{
	register_plugin(PLUGIN, VERSION, AUTHOR)
	register_cvar("amx_heatmap_version", VERSION, FCVAR_SERVER | FCVAR_SPONLY)
	g_cvEnable   = register_cvar("amx_heatmap_enable", "1")
	g_cvInterval = register_cvar("amx_heatmap_interval", "1.0")
	g_cvFlush    = register_cvar("amx_heatmap_flush", "60")
	g_cvBots     = register_cvar("amx_heatmap_bots", "1")
	g_cvGame     = register_cvar("amx_heatmap_game", "")
	g_cvHost     = register_cvar("amx_heatmap_sql_host", "127.0.0.1")
	g_cvUser     = register_cvar("amx_heatmap_sql_user", "hm_ingest")
	g_cvPass     = register_cvar("amx_heatmap_sql_pass", "", FCVAR_PROTECTED)
	g_cvDb       = register_cvar("amx_heatmap_sql_db", "heatmap")
	g_cvMax      = register_cvar("amx_heatmap_maxbuckets", "20000")

	g_index = TrieCreate()
	g_vals = ArrayCreate(10)
	g_counts = ArrayCreate(1)

	if (get_user_msgid("DeathMsg") > 0)
		register_event("DeathMsg", "ev_death", "a")
	register_event("ResetHUD", "ev_spawn", "b")
	register_concmd("amx_heatmap_status", "cmd_status", ADMIN_RCON, "- heatmap collector state")
}

public plugin_cfg()
{
	new cfg[128]
	get_configsdir(cfg, charsmax(cfg))
	server_cmd("exec %s/source_heatmaps.cfg", cfg)
	server_exec()

	// identity
	new game[32]
	get_pcvar_string(g_cvGame, game, charsmax(game))
	if (!game[0])
		get_modname(game, charsmax(game))
	sanitize(game, g_game, charsmax(g_game), true)
	new raw[64]
	get_mapname(raw, charsmax(raw))
	sanitize(raw, g_map, charsmax(g_map), true)
	g_port = get_cvar_num("port")
	g_tfc = equal(g_game, "tfc") ? true : false
	g_teamGame = g_tfc || get_cvar_num("mp_teamplay") != 0

	new host[64], user[64], pass[128], db[64]
	get_pcvar_string(g_cvHost, host, charsmax(host))
	get_pcvar_string(g_cvUser, user, charsmax(user))
	get_pcvar_string(g_cvPass, pass, charsmax(pass))
	get_pcvar_string(g_cvDb, db, charsmax(db))
	if (pass[0])
		g_tuple = SQL_MakeDbTuple(host, user, pass, db)
	else
		log_amx("[heatmap] amx_heatmap_sql_pass is empty; data is buffered but not sent")

	new Float:iv = get_pcvar_float(g_cvInterval)
	if (iv < 0.25) iv = 0.25
	new Float:fl = get_pcvar_float(g_cvFlush)
	if (fl < 10.0) fl = 10.0
	set_task(iv, "task_sample", TASK_SAMPLE, _, _, "b")
	set_task(fl, "task_flush", TASK_FLUSH, _, _, "b")
}

public plugin_end()
{
	// map change: flush synchronously so nothing is lost when the plugin unloads
	flush(true)
	if (g_tuple != Empty_Handle)
		SQL_FreeHandle(g_tuple)
	TrieDestroy(g_index)
	ArrayDestroy(g_vals)
	ArrayDestroy(g_counts)
}

/** keep [a-z0-9_.+-] (lower-cased); team names keep [A-Za-z0-9 _-] */
sanitize(const src[], dst[], maxlen, bool:mapname)
{
	new j = 0
	for (new i = 0; src[i] && j < maxlen; i++)
	{
		new c = src[i]
		if (mapname)
			c = tolower(c)
		if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-'
			|| (mapname && (c == '.' || c == '+')) || (!mapname && ((c >= 'A' && c <= 'Z') || c == ' ')))
			dst[j++] = c
	}
	dst[j] = 0
}

cell(Float:v, Float:size)
{
	new c = floatround(v / size, floatround_floor)
	if (c < -32000) c = -32000
	if (c > 32000) c = 32000
	return c
}

bool:in_world(const Float:o[3])
{
	return floatabs(o[0]) <= 32768.0 && floatabs(o[1]) <= 32768.0 && floatabs(o[2]) <= 32768.0
}

bump(kind, team, cls, bot, x, y, z, x2 = 0, y2 = 0, z2 = 0)
{
	new key[80], idx
	formatex(key, charsmax(key), "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d", kind, team, cls, bot, x, y, z, x2, y2, z2)
	if (TrieGetCell(g_index, key, idx))
	{
		ArraySetCell(g_counts, idx, ArrayGetCell(g_counts, idx) + 1)
		return
	}
	new vals[10]
	vals[0] = kind; vals[1] = team; vals[2] = cls; vals[3] = bot
	vals[4] = x; vals[5] = y; vals[6] = z; vals[7] = x2; vals[8] = y2; vals[9] = z2
	ArrayPushArray(g_vals, vals)
	ArrayPushCell(g_counts, 1)
	TrieSetCell(g_index, key, ArraySize(g_counts) - 1)
}

/** team index for storage (0 in free-for-all), and send its name once per map */
player_team(id)
{
	if (!g_teamGame)
		return 0
	new name[32], clean[32]
	new t = get_user_team(id, name, charsmax(name))
	if (t < 0 || t > 15)
		return 0
	if (!g_teamSent[t] && g_tuple != Empty_Handle && name[0])
	{
		sanitize(name, clean, charsmax(clean), false)
		if (clean[0])
		{
			new q[256]
			formatex(q, charsmax(q), "INSERT INTO hm_ingest_meta (port,game,map,kind,k,v) VALUES (%d,'%s','%s',1,%d,'%s')",
				g_port, g_game, g_map, t, clean)
			SQL_ThreadQuery(g_tuple, "on_query", q)
			g_teamSent[t] = true
		}
	}
	return t
}

player_class(id)
{
	if (!g_tfc)
		return 0
	new c = pev(id, pev_playerclass)
	return (c >= 0 && c < sizeof(TFC_CLASS)) ? TFC_CLASS[c] : 0
}

bool:trackable(id)
{
	if (!is_user_connected(id) || !is_user_alive(id) || is_user_hltv(id))
		return false
	if (is_user_bot(id) && !get_pcvar_num(g_cvBots))
		return false
	if (pev(id, pev_iuser1) != 0)      // observer / spectator mode
		return false
	if (g_tfc && pev(id, pev_playerclass) == 0)
		return false                   // TFC: not spawned as a class yet
	return true
}

feet(id, Float:o[3])
{
	new Float:mins[3]
	pev(id, pev_origin, o)
	pev(id, pev_mins, mins)
	o[2] += mins[2]
}

public task_sample()
{
	if (!get_pcvar_num(g_cvEnable) || !g_game[0])
		return
	new players[32], num
	get_players(players, num)
	new Float:o[3]
	for (new i = 0; i < num; i++)
	{
		new id = players[i]
		if (!trackable(id))
		{
			g_hasLast[id] = false
			continue
		}
		feet(id, o)
		if (!in_world(o))
		{
			g_hasLast[id] = false
			continue
		}
		new team = player_team(id), cls = player_class(id), bot = is_user_bot(id) ? 1 : 0
		new kind = ((pev(id, pev_flags) & FL_ONGROUND) || pev(id, pev_waterlevel) >= 2) ? KIND_GROUND : KIND_AIR
		bump(kind, team, cls, bot, cell(o[0], CELLSIZE), cell(o[1], CELLSIZE), cell(o[2], CELLSIZE))
		g_samples++
		if (g_hasLast[id])
		{
			new Float:d = get_distance_f(g_last[id], o)
			if (d >= 16.0 && d <= 1500.0)
			{
				new ax = cell(g_last[id][0], EDGESIZE), ay = cell(g_last[id][1], EDGESIZE), az = cell(g_last[id][2], EDGESIZE)
				new bx = cell(o[0], EDGESIZE), by = cell(o[1], EDGESIZE), bz = cell(o[2], EDGESIZE)
				if (ax != bx || ay != by || az != bz)
					bump(KIND_EDGE, team, cls, bot, ax, ay, az, bx, by, bz)
			}
		}
		g_last[id][0] = o[0]; g_last[id][1] = o[1]; g_last[id][2] = o[2]
		g_hasLast[id] = true
	}
	if (ArraySize(g_counts) >= get_pcvar_num(g_cvMax))
		flush(false)
}

public ev_spawn(id)
{
	g_hasLast[id] = false
}

public ev_death()
{
	new id = read_data(2)
	if (id < 1 || id > 32 || !is_user_connected(id) || is_user_hltv(id))
		return
	g_hasLast[id] = false
	if (!get_pcvar_num(g_cvEnable) || (is_user_bot(id) && !get_pcvar_num(g_cvBots)))
		return
	feet(id, g_deathPos[id])
	g_deathTeam[id] = player_team(id)
	g_deathCls[id] = player_class(id)
	// TFC spies can feign death: only count it if the player is really dead a moment later
	set_task(0.1, "task_death", TASK_DEATH + id)
}

public task_death(taskid)
{
	new id = taskid - TASK_DEATH
	if (!is_user_connected(id) || is_user_alive(id) || !in_world(g_deathPos[id]))
		return
	bump(KIND_DEATH, g_deathTeam[id], g_deathCls[id], is_user_bot(id) ? 1 : 0,
		cell(g_deathPos[id][0], CELLSIZE), cell(g_deathPos[id][1], CELLSIZE), cell(g_deathPos[id][2], CELLSIZE))
}

public task_flush()
{
	flush(false)
}

flush(bool:sync)
{
	new n = ArraySize(g_counts)
	if (!n)
		return
	if (g_tuple == Empty_Handle || !g_game[0] || !g_map[0])
	{
		if (n >= get_pcvar_num(g_cvMax))
			clear_buffer()
		return
	}
	new Handle:conn = Empty_Handle, err[256], errnum
	if (sync)
	{
		conn = SQL_Connect(g_tuple, errnum, err, charsmax(err))
		if (conn == Empty_Handle)
		{
			log_amx("[heatmap] connect failed at map end: %s", err)
			clear_buffer()
			return
		}
	}
	static q[12288]
	new len = 0, rows = 0, vals[10]
	for (new i = 0; i < n; i++)
	{
		ArrayGetArray(g_vals, i, vals)
		if (!rows)
			len = formatex(q, charsmax(q), "INSERT INTO hm_ingest (port,game,map,kind,team,cls,bot,x,y,z,x2,y2,z2,n) VALUES ")
		len += formatex(q[len], charsmax(q) - len, "%s(%d,'%s','%s',%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d)", rows ? "," : "",
			g_port, g_game, g_map, vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], vals[6], vals[7], vals[8], vals[9],
			ArrayGetCell(g_counts, i))
		rows++
		if (rows >= ROWS_PER_QUERY || i == n - 1)
		{
			send(q, sync, conn)
			rows = 0
		}
	}
	if (conn != Empty_Handle)
		SQL_FreeHandle(conn)
	clear_buffer()
}

send(const q[], bool:sync, Handle:conn)
{
	if (!sync)
	{
		SQL_ThreadQuery(g_tuple, "on_query", q)
		return
	}
	new Handle:h = SQL_PrepareQuery(conn, "%s", q)
	if (!SQL_Execute(h))
	{
		new err[256]
		SQL_QueryError(h, err, charsmax(err))
		g_errors++
		log_amx("[heatmap] insert failed at map end: %s", err)
	}
	SQL_FreeHandle(h)
}

clear_buffer()
{
	TrieClear(g_index)
	ArrayClear(g_vals)
	ArrayClear(g_counts)
	g_samples = 0
}

public on_query(failstate, Handle:query, error[], errnum, data[], size, Float:queuetime)
{
	if (failstate != TQUERY_SUCCESS)
	{
		g_errors++
		if (g_errors <= 5 || g_errors % 100 == 0)
			log_amx("[heatmap] insert failed (%d so far): %s", g_errors, error)
	}
}

public cmd_status(id, level, cid)
{
	if (!cmd_access(id, level, cid, 1))
		return PLUGIN_HANDLED
	console_print(id, "[heatmap] v%s game=%s map=%s port=%d db=%s buckets=%d samples=%d errors=%d teamgame=%d",
		VERSION, g_game, g_map, g_port, g_tuple != Empty_Handle ? "configured" : "none", ArraySize(g_counts), g_samples,
		g_errors, g_teamGame)
	new players[32], num, name[32]
	get_players(players, num)
	for (new i = 0; i < num; i++)
	{
		new p = players[i]
		get_user_name(p, name, charsmax(name))
		console_print(id, "  #%d %s team=%d raw=%d cls=%d bot=%d alive=%d", p, name, player_team(p),
			g_tfc ? pev(p, pev_playerclass) : 0, player_class(p), is_user_bot(p), is_user_alive(p))
	}
	return PLUGIN_HANDLED
}
