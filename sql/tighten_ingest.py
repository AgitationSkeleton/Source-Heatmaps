"""Switch an existing hm_ingest account to column-level INSERT grants (no password change).

Older setups granted INSERT on the whole staging tables, which also lets the account set the
AUTO_INCREMENT id and the received timestamp. Run once as a MySQL admin:

  python -I tighten_ingest.py --mysql /path/to/mysql [--admin root] [--ingest-host HOST ...] [--close-test-db]
"""
import argparse
import getpass
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup_db import ingest_grants, sql_str  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mysql", default="mysql")
    ap.add_argument("--admin", default="root")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default="3306")
    ap.add_argument("--ingest-host", action="append", default=[])
    ap.add_argument("--close-test-db", action="store_true",
                    help="also remove MariaDB's default grants that let every account use the `test` / `test_%%` "
                         "databases (removes grants only, no data)")
    a = ap.parse_args()
    for h in a.ingest_host:
        if not re.fullmatch(r"[A-Za-z0-9.\-:%_]{1,60}", h):
            sys.exit("bad --ingest-host: %r" % h)
    pw = getpass.getpass("MySQL %s password: " % a.admin)
    stmts = []
    for h in ["localhost", "127.0.0.1"] + a.ingest_host:
        acct = "%s@%s" % (sql_str("hm_ingest"), sql_str(h))
        stmts.append("REVOKE INSERT ON `heatmap`.`hm_ingest` FROM %s;" % acct)
        stmts.append("REVOKE INSERT ON `heatmap`.`hm_ingest_meta` FROM %s;" % acct)
        stmts += ingest_grants(acct)
    if a.close_test_db:
        stmts.append("DELETE FROM mysql.db WHERE Db IN ('test', 'test\\_%') AND User = '';")
    stmts += ["FLUSH PRIVILEGES;", "SHOW GRANTS FOR 'hm_ingest'@'localhost';", "SELECT 'tighten-ok';"]
    p = subprocess.run([a.mysql, "-h", a.host, "-P", a.port, "-u", a.admin, "-N", "--batch"],
                       input="\n".join(stmts), env=dict(os.environ, MYSQL_PWD=pw), capture_output=True, text=True)
    if p.returncode != 0 or "tighten-ok" not in p.stdout:
        sys.exit("mysql failed (exit %d): %s" % (p.returncode, p.stderr.strip()[:500]))
    print("hm_ingest now has column-level INSERT only:")
    for line in p.stdout.splitlines():
        if line.startswith("GRANT"):
            print("  " + re.sub(r"IDENTIFIED BY PASSWORD '[^']*'", "IDENTIFIED BY PASSWORD '***'", line))


if __name__ == "__main__":
    main()
