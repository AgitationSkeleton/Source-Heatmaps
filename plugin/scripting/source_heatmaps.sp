/**
 * Source Heatmaps - positional telemetry for Source engine servers
 *
 * Samples every living player and bot once per interval, buckets the samples in memory
 * (32-unit cells for presence and deaths, 64-unit cells for movement edges) and flushes the
 * buckets to MySQL as append-only staging rows. The website's exporter validates and folds them.
 *
 * The database account only needs INSERT on hm_ingest and hm_ingest_meta (see sql/setup_db.py).
 *
 * Canonical classes (cls column):
 *   0 none, 1 scout, 2 soldier, 3 pyro, 4 demoman, 5 heavy, 6 engineer, 7 medic, 8 sniper,
 *   9 spy, 10 civilian, 11 mercenary, 12 zombie, 13 juggernaut
 *
 * Special labels:
 *   - tf2cc (library "tf2cc"): role keys containing "merc" -> mercenary, "civ" -> civilian
 *   - tf2cc on TF2C: models/player/tf2cc/merc.mdl -> mercenary
 *   - TF2C DM/infection VScript bundle: merc_deathmatch model or soldier -> mercenary,
 *     skeleton_zombie model or sniper -> zombie
 *
 * Supports SourceMod 1.11+. No extensions beyond the bundled MySQL driver.
 */
#pragma semicolon 1
#pragma newdecls required
#pragma dynamic 65536

#include <sourcemod>
#include <sdktools>

#define PLUGIN_VERSION "1.0.2"

#define KIND_GROUND  0
#define KIND_DEATH   1
#define KIND_EDGE    2
#define KIND_AIR     3

#define CELL         32.0
#define EDGE_CELL    64.0
#define ROWS_PER_QUERY 250

#define CLS_NONE     0
#define CLS_SOLDIER  2
#define CLS_SNIPER   8
#define CLS_CIVILIAN 10
#define CLS_MERC     11
#define CLS_ZOMBIE   12
#define CLS_JUGG     13

public Plugin myinfo =
{
	name        = "Source Heatmaps",
	author      = "Vio's Arcade",
	description = "Collects player/bot positions by team and class for heatmaps and nav analysis",
	version     = PLUGIN_VERSION,
	url         = "https://github.com/AgitationSkeleton/Source-Heatmaps"
};

// tf2cc (optional)
native int TF2CC_GetRole(int client);
native void TF2CC_GetRoleKey(int role, char[] buffer, int maxlen);

ConVar g_cvEnable, g_cvInterval, g_cvFlush, g_cvBots, g_cvGame, g_cvClassMap, g_cvDbConfig, g_cvMaxBuckets;

Database g_hDb;
bool g_bConnecting;
bool g_bReloadedDbs;
StringMap g_hBuf;
int g_iBufSamples;
int g_iDropped;
int g_iErrors;

char g_sGame[32];
char g_sMap[96];
int g_iPort;
bool g_bMetaSent;

int g_iClassMap[32];
bool g_bTF2CC;
bool g_bBundle;
bool g_bTF2C;
int g_iClassMode;          // 0 none, 1 m_iClass, 2 FF m_iClassStatus

float g_vLast[MAXPLAYERS + 1][3];
bool g_bHasLast[MAXPLAYERS + 1];

Handle g_hSampleTimer, g_hFlushTimer;

public APLRes AskPluginLoad2(Handle myself, bool late, char[] error, int err_max)
{
	MarkNativeAsOptional("TF2CC_GetRole");
	MarkNativeAsOptional("TF2CC_GetRoleKey");
	return APLRes_Success;
}

public void OnPluginStart()
{
	CreateConVar("sm_heatmap_version", PLUGIN_VERSION, "Source Heatmaps version", FCVAR_NOTIFY | FCVAR_DONTRECORD);
	g_cvEnable     = CreateConVar("sm_heatmap_enable", "1", "Collect heatmap data.", _, true, 0.0, true, 1.0);
	g_cvInterval   = CreateConVar("sm_heatmap_interval", "1.0", "Seconds between position samples.", _, true, 0.25, true, 10.0);
	g_cvFlush      = CreateConVar("sm_heatmap_flush", "60", "Seconds between database flushes.", _, true, 10.0, true, 600.0);
	g_cvBots       = CreateConVar("sm_heatmap_bots", "1", "Also record bots (stored with bot=1).", _, true, 0.0, true, 1.0);
	g_cvGame       = CreateConVar("sm_heatmap_game", "", "Game key sent to the database (empty = game folder name).");
	g_cvClassMap   = CreateConVar("sm_heatmap_classmap", "", "Override raw->canonical class mapping, e.g. \"10:11,11:10\".");
	g_cvDbConfig   = CreateConVar("sm_heatmap_dbconfig", "heatmap", "databases.cfg section to use.");
	g_cvMaxBuckets = CreateConVar("sm_heatmap_maxbuckets", "40000", "Flush early when this many buckets are buffered.", _, true, 1000.0, true, 200000.0);
	AutoExecConfig(true, "source_heatmaps");

	g_cvInterval.AddChangeHook(OnTimerCvarChanged);
	g_cvFlush.AddChangeHook(OnTimerCvarChanged);
	g_cvGame.AddChangeHook(OnIdentityCvarChanged);
	g_cvClassMap.AddChangeHook(OnIdentityCvarChanged);

	g_hBuf = new StringMap();

	HookEventEx("player_death", Event_PlayerDeath, EventHookMode_Post);
	HookEventEx("player_spawn", Event_PlayerSpawn, EventHookMode_Post);
	HookEventEx("teamplay_round_start", Event_RoundStart, EventHookMode_PostNoCopy);

	RegAdminCmd("sm_heatmap_status", Cmd_Status, ADMFLAG_RCON, "Show heatmap collector state and how each player is labelled.");
	RegAdminCmd("sm_heatmap_flushnow", Cmd_FlushNow, ADMFLAG_RCON, "Flush buffered heatmap data now.");

	ConVar hp = FindConVar("hostport");
	g_iPort = hp != null ? hp.IntValue : 0;

	ResolveIdentity();
	Connect();
}

public void OnAllPluginsLoaded()
{
	g_bTF2CC = LibraryExists("tf2cc") && GetFeatureStatus(FeatureType_Native, "TF2CC_GetRole") == FeatureStatus_Available;
}

public void OnLibraryAdded(const char[] name)
{
	if (StrEqual(name, "tf2cc"))
		g_bTF2CC = GetFeatureStatus(FeatureType_Native, "TF2CC_GetRole") == FeatureStatus_Available;
}

public void OnLibraryRemoved(const char[] name)
{
	if (StrEqual(name, "tf2cc"))
		g_bTF2CC = false;
}

public void OnPluginEnd()
{
	Flush();
}

public void OnMapStart()
{
	char raw[PLATFORM_MAX_PATH];
	GetCurrentMap(raw, sizeof(raw));
	if (!GetMapDisplayName(raw, g_sMap, sizeof(g_sMap)))
		strcopy(g_sMap, sizeof(g_sMap), raw);
	for (int i = 0; g_sMap[i]; i++)
		g_sMap[i] = CharToLower(g_sMap[i]);
	g_bMetaSent = false;
	for (int i = 1; i <= MaxClients; i++)
		g_bHasLast[i] = false;
	DetectBundle();
	CreateTimer(2.0, Timer_DetectBundle, _, TIMER_FLAG_NO_MAPCHANGE);
	RestartTimers();
}

public void OnConfigsExecuted()
{
	ResolveIdentity();
	RestartTimers();
	SendMeta();
}

public void OnMapEnd()
{
	Flush();
	g_hSampleTimer = null;   // TIMER_FLAG_NO_MAPCHANGE closes them
	g_hFlushTimer = null;
}

public void OnClientDisconnect(int client)
{
	g_bHasLast[client] = false;
}

void OnTimerCvarChanged(ConVar cv, const char[] o, const char[] n)
{
	RestartTimers();
}

void OnIdentityCvarChanged(ConVar cv, const char[] o, const char[] n)
{
	Flush();
	ResolveIdentity();
	g_bMetaSent = false;
	SendMeta();
}

void RestartTimers()
{
	delete g_hSampleTimer;
	delete g_hFlushTimer;
	g_hSampleTimer = CreateTimer(g_cvInterval.FloatValue, Timer_Sample, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
	g_hFlushTimer = CreateTimer(g_cvFlush.FloatValue, Timer_Flush, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
}

// ---------------------------------------------------------------------------------------------
// identity / classes
// ---------------------------------------------------------------------------------------------

void ResolveIdentity()
{
	char game[32];
	g_cvGame.GetString(game, sizeof(game));
	TrimString(game);
	if (!game[0])
		GetGameFolderName(game, sizeof(game));
	// keep only [a-z0-9_]; the exporter rejects anything else
	int j = 0;
	for (int i = 0; game[i] && j < sizeof(g_sGame) - 1; i++)
	{
		int c = CharToLower(game[i]);
		if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')
			g_sGame[j++] = c;
	}
	g_sGame[j] = '\0';

	for (int i = 0; i < sizeof(g_iClassMap); i++)
		g_iClassMap[i] = CLS_NONE;

	g_iClassMode = 0;
	g_bTF2C = StrEqual(g_sGame, "tf2classified") || StrEqual(g_sGame, "tf2classic");
	if (StrEqual(g_sGame, "fortressforever"))
		g_iClassMode = 2;
	else if (StrEqual(g_sGame, "tf") || StrEqual(g_sGame, "tf2classified") || StrEqual(g_sGame, "tf2classic")
		|| StrEqual(g_sGame, "open_fortress") || StrEqual(g_sGame, "pf2") || StrEqual(g_sGame, "tf_goldrush"))
		g_iClassMode = 1;

	if (g_iClassMode != 0)
	{
		// TF_CLASS_* order, shared by TF2, TF2C, PF2, Gold Rush, Open Fortress and FF (hwguy = heavy)
		g_iClassMap[1] = 1;   // scout
		g_iClassMap[2] = 8;   // sniper
		g_iClassMap[3] = 2;   // soldier
		g_iClassMap[4] = 4;   // demoman
		g_iClassMap[5] = 7;   // medic
		g_iClassMap[6] = 5;   // heavy
		g_iClassMap[7] = 3;   // pyro
		g_iClassMap[8] = 9;   // spy
		g_iClassMap[9] = 6;   // engineer
		g_iClassMap[10] = CLS_CIVILIAN;
		g_iClassMap[11] = CLS_MERC;
		if (StrEqual(g_sGame, "open_fortress"))
		{
			g_iClassMap[10] = CLS_MERC;
			g_iClassMap[11] = CLS_CIVILIAN;
			g_iClassMap[12] = CLS_JUGG;
		}
	}

	// manual overrides "raw:canonical,raw:canonical"
	char spec[256], pairs[32][16], kv[2][8];
	g_cvClassMap.GetString(spec, sizeof(spec));
	int n = ExplodeString(spec, ",", pairs, sizeof(pairs), sizeof(pairs[]));
	for (int i = 0; i < n; i++)
	{
		if (ExplodeString(pairs[i], ":", kv, sizeof(kv), sizeof(kv[])) != 2)
			continue;
		int r = StringToInt(kv[0]), c = StringToInt(kv[1]);
		if (r >= 0 && r < sizeof(g_iClassMap) && c >= 0 && c <= CLS_JUGG)
			g_iClassMap[r] = c;
	}
}

public Action Timer_DetectBundle(Handle t)
{
	DetectBundle();
	return Plugin_Stop;
}

public void Event_RoundStart(Event e, const char[] name, bool dontBroadcast)
{
	CreateTimer(1.0, Timer_DetectBundle, _, TIMER_FLAG_NO_MAPCHANGE);
}

/** TF2C DM/infection VScript bundle: a logic_script that runs the merc scripts. */
void DetectBundle()
{
	g_bBundle = false;
	if (!g_bTF2C)
		return;
	int ent = -1;
	char scripts[512];
	while ((ent = FindEntityByClassname(ent, "logic_script")) != -1)
	{
		if (!HasEntProp(ent, Prop_Data, "m_iszVScripts"))
			continue;
		GetEntPropString(ent, Prop_Data, "m_iszVScripts", scripts, sizeof(scripts));
		if (StrContains(scripts, "merc_soldieronly", false) != -1 || StrContains(scripts, "tf2c_infection", false) != -1)
		{
			g_bBundle = true;
			return;
		}
	}
}

int RawClass(int client)
{
	if (g_iClassMode == 1 && HasEntProp(client, Prop_Send, "m_iClass"))
		return GetEntProp(client, Prop_Send, "m_iClass");
	if (g_iClassMode == 2 && HasEntProp(client, Prop_Send, "m_iClassStatus"))
		return GetEntProp(client, Prop_Send, "m_iClassStatus") & 0xF;
	return 0;
}

int CanonicalClass(int client)
{
	if (g_iClassMode == 0)
		return CLS_NONE;
	int raw = RawClass(client);
	int cls = (raw >= 0 && raw < sizeof(g_iClassMap)) ? g_iClassMap[raw] : CLS_NONE;

	if (g_bTF2CC)
	{
		int role = TF2CC_GetRole(client);
		if (role >= 0)
		{
			char key[32];
			TF2CC_GetRoleKey(role, key, sizeof(key));
			if (StrContains(key, "merc", false) != -1)
				return CLS_MERC;
			if (StrContains(key, "civ", false) != -1)
				return CLS_CIVILIAN;
		}
	}

	// TF2C DM/infection bundle: the custom model says Merc or Zombie (checked on every TF2C sample,
	// so it is right even before the bundle is detected after a map change)
	if (g_bTF2C)
	{
		if (HasEntProp(client, Prop_Send, "m_iszCustomModel"))
		{
			char model[PLATFORM_MAX_PATH];
			GetEntPropString(client, Prop_Send, "m_iszCustomModel", model, sizeof(model));
			if (StrContains(model, "skeleton_zombie", false) != -1)
				return CLS_ZOMBIE;
			// DM bundle merc, or tf2cc's TF2C Merc (models/player/tf2cc/merc.mdl; tf2cc on TF2C has no natives)
			if (StrContains(model, "merc_deathmatch", false) != -1 || StrContains(model, "tf2cc/merc", false) != -1)
				return CLS_MERC;
		}
		if (g_bBundle && cls == CLS_SOLDIER)
			return CLS_MERC;
		if (g_bBundle && cls == CLS_SNIPER)
			return CLS_ZOMBIE;
	}
	return cls;
}

bool Trackable(int client)
{
	if (!IsClientInGame(client) || !IsPlayerAlive(client))
		return false;
	if (IsClientSourceTV(client) || IsClientReplay(client))
		return false;
	if (IsFakeClient(client) && !g_cvBots.BoolValue)
		return false;
	return GetClientTeam(client) != 1;   // spectators
}

// ---------------------------------------------------------------------------------------------
// sampling
// ---------------------------------------------------------------------------------------------

bool InWorld(const float pos[3])
{
	return FloatAbs(pos[0]) <= 32768.0 && FloatAbs(pos[1]) <= 32768.0 && FloatAbs(pos[2]) <= 32768.0;
}

int Cell(float v, float size)
{
	int c = RoundToFloor(v / size);
	if (c < -32000) c = -32000;
	if (c > 32000) c = 32000;
	return c;
}

void Bump(int kind, int team, int cls, int bot, int x, int y, int z, int x2 = 0, int y2 = 0, int z2 = 0, int add = 1)
{
	char key[80];
	FormatEx(key, sizeof(key), "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d", kind, team, cls, bot, x, y, z, x2, y2, z2);
	int cur = 0;
	g_hBuf.GetValue(key, cur);
	g_hBuf.SetValue(key, cur + add);
}

public Action Timer_Sample(Handle t)
{
	if (!g_cvEnable.BoolValue || !g_sGame[0])
		return Plugin_Continue;

	float pos[3];
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!Trackable(i))
		{
			g_bHasLast[i] = false;
			continue;
		}
		GetClientAbsOrigin(i, pos);
		if (!InWorld(pos))
		{
			g_bHasLast[i] = false;   // fell out of the map (or teleported to a holding area)
			continue;
		}
		int team = GetClientTeam(i);
		int cls = CanonicalClass(i);
		int bot = IsFakeClient(i) ? 1 : 0;
		int flags = GetEntityFlags(i);
		int kind = (flags & (FL_ONGROUND | FL_INWATER)) ? KIND_GROUND : KIND_AIR;
		Bump(kind, team, cls, bot, Cell(pos[0], CELL), Cell(pos[1], CELL), Cell(pos[2], CELL));
		g_iBufSamples++;

		if (g_bHasLast[i])
		{
			float d = GetVectorDistance(g_vLast[i], pos);
			if (d >= 16.0 && d <= 1500.0)
			{
				int ax = Cell(g_vLast[i][0], EDGE_CELL), ay = Cell(g_vLast[i][1], EDGE_CELL), az = Cell(g_vLast[i][2], EDGE_CELL);
				int bx = Cell(pos[0], EDGE_CELL), by = Cell(pos[1], EDGE_CELL), bz = Cell(pos[2], EDGE_CELL);
				if (ax != bx || ay != by || az != bz)
					Bump(KIND_EDGE, team, cls, bot, ax, ay, az, bx, by, bz);
			}
		}
		g_vLast[i] = pos;
		g_bHasLast[i] = true;
	}

	if (g_hBuf.Size >= g_cvMaxBuckets.IntValue)
		Flush();
	return Plugin_Continue;
}

public void Event_PlayerSpawn(Event e, const char[] name, bool dontBroadcast)
{
	int client = GetClientOfUserId(e.GetInt("userid"));
	if (client > 0)
		g_bHasLast[client] = false;
}

public void Event_PlayerDeath(Event e, const char[] name, bool dontBroadcast)
{
	int client = GetClientOfUserId(e.GetInt("userid"));
	if (client <= 0 || !IsClientInGame(client))
		return;
	g_bHasLast[client] = false;
	if (!g_cvEnable.BoolValue || !g_sGame[0] || IsClientSourceTV(client) || IsClientReplay(client))
		return;
	if (IsFakeClient(client) && !g_cvBots.BoolValue)
		return;
	int team = GetClientTeam(client);
	if (team == 1)
		return;
	// FF and some mods fire player_death for feign death; only count real deaths a frame later
	DataPack dp;
	CreateDataTimer(0.1, Timer_Death, dp, TIMER_FLAG_NO_MAPCHANGE);
	float pos[3];
	GetClientAbsOrigin(client, pos);
	dp.WriteCell(GetClientUserId(client));
	dp.WriteCell(team);
	dp.WriteCell(CanonicalClass(client));
	dp.WriteFloat(pos[0]);
	dp.WriteFloat(pos[1]);
	dp.WriteFloat(pos[2]);
}

public Action Timer_Death(Handle t, DataPack dp)
{
	dp.Reset();
	int client = GetClientOfUserId(dp.ReadCell());
	int team = dp.ReadCell();
	int cls = dp.ReadCell();
	float pos[3];
	pos[0] = dp.ReadFloat();
	pos[1] = dp.ReadFloat();
	pos[2] = dp.ReadFloat();
	if (client <= 0 || !IsClientInGame(client) || IsPlayerAlive(client) || !InWorld(pos))
		return Plugin_Stop;   // left, not actually dead (feign death), or outside the map
	Bump(KIND_DEATH, team, cls, IsFakeClient(client) ? 1 : 0, Cell(pos[0], CELL), Cell(pos[1], CELL), Cell(pos[2], CELL));
	return Plugin_Stop;
}

// ---------------------------------------------------------------------------------------------
// database
// ---------------------------------------------------------------------------------------------

void Connect()
{
	if (g_hDb != null || g_bConnecting)
		return;
	char cfg[64];
	g_cvDbConfig.GetString(cfg, sizeof(cfg));
	if (!SQL_CheckConfig(cfg))
	{
		if (!g_bReloadedDbs)
		{
			// a databases.cfg entry added while the server was running: load it once
			g_bReloadedDbs = true;
			ServerCommand("sm_reload_databases");
			CreateTimer(3.0, Timer_Reconnect);
			return;
		}
		LogError("databases.cfg has no \"%s\" section; heatmap data is buffered but not sent.", cfg);
		CreateTimer(120.0, Timer_Reconnect);
		return;
	}
	g_bConnecting = true;
	Database.Connect(OnConnected, cfg);
}

public Action Timer_Reconnect(Handle t)
{
	Connect();
	return Plugin_Stop;
}

public void OnConnected(Database db, const char[] error, any data)
{
	g_bConnecting = false;
	if (db == null)
	{
		LogError("Database connect failed: %s (retrying in 60 s)", error);
		CreateTimer(60.0, Timer_Reconnect);
		return;
	}
	g_hDb = db;
	g_hDb.SetCharset("utf8mb4");
	g_bMetaSent = false;
	SendMeta();
}

void SendMeta()
{
	if (g_hDb == null || g_bMetaSent || !g_sMap[0] || !g_sGame[0])
		return;
	int teams = GetTeamCount();
	if (teams <= 0)
		return;
	char q[4096], esc[200], game[80], map[200], name[64];
	g_hDb.Escape(g_sGame, game, sizeof(game));
	g_hDb.Escape(g_sMap, map, sizeof(map));
	FormatEx(q, sizeof(q), "INSERT INTO hm_ingest_meta (port,game,map,kind,k,v) VALUES ");
	bool first = true;
	for (int t = 0; t < teams && t < 16; t++)
	{
		GetTeamName(t, name, sizeof(name));
		g_hDb.Escape(name, esc, sizeof(esc));
		Format(q, sizeof(q), "%s%s(%d,'%s','%s',1,%d,'%s')", q, first ? "" : ",", g_iPort, game, map, t, esc);
		first = false;
	}
	g_hDb.Query(OnInsert, q);
	g_bMetaSent = true;
}

public Action Timer_Flush(Handle t)
{
	SendMeta();
	Flush();
	return Plugin_Continue;
}

void Flush()
{
	if (g_hBuf == null || g_hBuf.Size == 0)
		return;
	if (g_hDb == null || !g_sGame[0] || !g_sMap[0])
	{
		// keep buffering until the cap, then drop (never grow without bound)
		if (g_hBuf.Size >= g_cvMaxBuckets.IntValue)
		{
			g_iDropped += g_hBuf.Size;
			g_hBuf.Clear();
		}
		return;
	}

	static char q[65536];
	char game[80], map[200], key[80], parts[10][12];
	g_hDb.Escape(g_sGame, game, sizeof(game));
	g_hDb.Escape(g_sMap, map, sizeof(map));

	StringMapSnapshot snap = g_hBuf.Snapshot();
	int rows = 0, len = 0;
	for (int i = 0; i < snap.Length; i++)
	{
		snap.GetKey(i, key, sizeof(key));
		int n;
		if (!g_hBuf.GetValue(key, n) || n <= 0)
			continue;
		if (ExplodeString(key, ",", parts, sizeof(parts), sizeof(parts[])) != 10)
			continue;
		if (rows == 0)
			len = FormatEx(q, sizeof(q), "INSERT INTO hm_ingest (port,game,map,kind,team,cls,bot,x,y,z,x2,y2,z2,n) VALUES ");
		len += FormatEx(q[len], sizeof(q) - len, "%s(%d,'%s','%s',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%d)",
			rows ? "," : "", g_iPort, game, map,
			parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], parts[7], parts[8], parts[9], n);
		rows++;
		if (rows >= ROWS_PER_QUERY || len > sizeof(q) - 600)
		{
			g_hDb.Query(OnInsert, q, _, DBPrio_Low);
			rows = 0;
		}
	}
	if (rows > 0)
		g_hDb.Query(OnInsert, q, _, DBPrio_Low);
	delete snap;
	g_hBuf.Clear();
	g_iBufSamples = 0;
}

public void OnInsert(Database db, DBResultSet rs, const char[] error, any data)
{
	if (rs == null)
	{
		g_iErrors++;
		if (g_iErrors <= 5 || g_iErrors % 100 == 0)
			LogError("Insert failed (%d so far): %s", g_iErrors, error);
	}
}

public Action Cmd_FlushNow(int client, int args)
{
	int n = g_hBuf.Size;
	SendMeta();
	Flush();
	ReplyToCommand(client, "[heatmap] flushed %d bucket(s)%s", n, g_hDb == null ? " (no database connection; kept)" : "");
	return Plugin_Handled;
}

public Action Cmd_Status(int client, int args)
{
	ReplyToCommand(client, "[heatmap] v%s game=%s map=%s port=%d db=%s buckets=%d samples=%d dropped=%d errors=%d tf2cc=%d bundle=%d classmode=%d",
		PLUGIN_VERSION, g_sGame, g_sMap, g_iPort, g_hDb != null ? "connected" : (g_bConnecting ? "connecting" : "none"),
		g_hBuf.Size, g_iBufSamples, g_iDropped, g_iErrors, g_bTF2CC, g_bBundle, g_iClassMode);
	char tn[32];
	for (int t = 0; t < GetTeamCount() && t < 16; t++)
	{
		GetTeamName(t, tn, sizeof(tn));
		ReplyToCommand(client, "  team %d = %s", t, tn);
	}
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsClientInGame(i) || IsClientSourceTV(i) || IsClientReplay(i))
			continue;
		char model[PLATFORM_MAX_PATH] = "";
		if (HasEntProp(i, Prop_Send, "m_iszCustomModel"))
			GetEntPropString(i, Prop_Send, "m_iszCustomModel", model, sizeof(model));
		ReplyToCommand(client, "  #%d %N team=%d raw=%d cls=%d bot=%d alive=%d %s", i, i, GetClientTeam(i), RawClass(i),
			CanonicalClass(i), IsFakeClient(i), IsPlayerAlive(i), model);
	}
	return Plugin_Handled;
}
