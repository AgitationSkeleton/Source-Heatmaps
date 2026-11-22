"""One-time database setup for Source Heatmaps.

Creates the `heatmap` database (sql/schema.sql) and two least-privilege accounts:

  hm_ingest  game servers: INSERT on heatmap.hm_ingest and heatmap.hm_ingest_meta only
  hm_export  exporter job: SELECT/INSERT/UPDATE/DELETE on heatmap.* only

Both accounts are local only (localhost and 127.0.0.1). Their passwords are random, written to
the secrets file (default: ../secrets/db.json next to this script) and never printed. The admin
password is asked for interactively, handed to the mysql client through its environment, and not
stored. Re-running drops and recreates both accounts with new passwords (and rewrites the secrets file).

usage: python -I setup_db.py [--mysql PATH_TO_mysql.exe] [--admin root] [--host 127.0.0.1]
                             [--port 3306] [--secrets PATH] [--ingest-host HOST ...]

--ingest-host adds extra hosts for hm_ingest, for game servers on another machine (for example
192.0.2.20). Only add hosts you control; prefer a firewall rule that limits port 3306 to them.
"""
import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sql_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


INGEST_COLS = "`port`,`game`,`map`,`kind`,`team`,`cls`,`bot`,`x`,`y`,`z`,`x2`,`y2`,`z2`,`n`"
META_COLS = "`port`,`game`,`map`,`kind`,`k`,`v`"


def ingest_grants(acct):
    """Column-level INSERT only: the ingest account can't set id (AUTO_INCREMENT) or received."""
    return ["GRANT INSERT (%s) ON `heatmap`.`hm_ingest` TO %s;" % (INGEST_COLS, acct),
            "GRANT INSERT (%s) ON `heatmap`.`hm_ingest_meta` TO %s;" % (META_COLS, acct)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mysql", default="mysql", help="path to the mysql command-line client (XAMPP: <xampp>/mysql/bin/mysql.exe)")
    ap.add_argument("--admin", default="root")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default="3306")
    ap.add_argument("--secrets", default=os.path.join(os.path.dirname(HERE), "secrets", "db.json"))
    ap.add_argument("--ingest-host", action="append", default=[])
    a = ap.parse_args()

    for h in a.ingest_host:
        if not re.fullmatch(r"[A-Za-z0-9.\-:%_]{1,60}", h):
            sys.exit("bad --ingest-host: %r" % h)

    pw_admin = getpass.getpass("MySQL %s password: " % a.admin)
    ingest_pw = secrets.token_urlsafe(24)
    export_pw = secrets.token_urlsafe(24)

    schema = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    stmts = [schema]
    ingest_hosts = ["localhost", "127.0.0.1"] + a.ingest_host
    for user, pw, hosts in (("hm_ingest", ingest_pw, ingest_hosts), ("hm_export", export_pw, ["localhost", "127.0.0.1"])):
        for h in hosts:
            acct = "%s@%s" % (sql_str(user), sql_str(h))
            stmts.append("DROP USER IF EXISTS %s;" % acct)
            stmts.append("CREATE USER %s IDENTIFIED BY %s;" % (acct, sql_str(pw)))
            if user == "hm_ingest":
                stmts += ingest_grants(acct)
                stmts.append("ALTER USER %s WITH MAX_USER_CONNECTIONS 64;" % acct)
            else:
                stmts.append("GRANT SELECT, INSERT, UPDATE, DELETE ON `heatmap`.* TO %s;" % acct)
    stmts.append("FLUSH PRIVILEGES;")
    stmts.append("SELECT 'setup-ok';")

    env = dict(os.environ, MYSQL_PWD=pw_admin)
    p = subprocess.run([a.mysql, "-h", a.host, "-P", a.port, "-u", a.admin, "-N", "--batch"],
                       input="\n".join(stmts), env=env, capture_output=True, text=True)
    if p.returncode != 0 or "setup-ok" not in p.stdout:
        # mysql error text never contains the passwords we generated (they only appear in the input)
        sys.exit("mysql failed (exit %d): %s" % (p.returncode, p.stderr.strip()[:500]))

    os.makedirs(os.path.dirname(a.secrets), exist_ok=True)
    data = {
        "host": a.host, "port": int(a.port), "database": "heatmap",
        "ingest": {"user": "hm_ingest", "pass": ingest_pw},
        "export": {"user": "hm_export", "pass": export_pw},
    }
    with open(a.secrets, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if os.name == "nt":
        # owner-only: Administrators + SYSTEM
        subprocess.run(["icacls", a.secrets, "/inheritance:r", "/grant:r", "*S-1-5-32-544:F", "*S-1-5-18:F"],
                       capture_output=True)
    else:
        os.chmod(a.secrets, 0o600)
    print("Database 'heatmap' ready. Accounts hm_ingest and hm_export (re)created.")
    print("Passwords written to %s (not shown)." % a.secrets)


if __name__ == "__main__":
    main()
