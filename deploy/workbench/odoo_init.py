"""Initialize only this installation's Odoo database; preserve it on restart."""
from __future__ import annotations

import configparser
import json
import subprocess
import sys
from pathlib import Path

import psycopg2

CONFIG = Path('/etc/forgeflow/odoo.conf')


def main() -> int:
    config = configparser.ConfigParser(interpolation=None)
    config.read(CONFIG)
    options = config['options']
    database = options['db_name']
    if database != 'forgeflow_local':
        raise ValueError('unexpected_local_database')
    with psycopg2.connect(host=options['db_host'], port=options.getint('db_port', 5432),
                          user=options['db_user'], password=options['db_password'],
                          dbname='postgres') as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,))
            exists = cursor.fetchone() is not None
    if not exists:
        subprocess.run(['odoo', 'db', '-c', str(CONFIG), 'init', database], check=True)
    # Odoo's module operation is transactional and may be retried after a failed
    # first boot. The seed below refuses unrelated databases and never resets an
    # existing installation's business records or operator permissions.
    subprocess.run(['odoo', '-c', str(CONFIG), '-i', 'forgeflow_general',
                    '--without-demo', '--stop-after-init', '--no-http'], check=True)
    subprocess.run(['odoo', 'shell', '-c', str(CONFIG), '--no-http'],
                   input="exec(open('/opt/forgeflow/odoo_bootstrap.py').read())\n",
                   text=True, check=True)
    print(json.dumps({'ok': True, 'stage': 'odoo_initialized'}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Never echo a connection string, command environment or credentials.
        print(json.dumps({'ok': False, 'stage': 'odoo_init',
                          'error_type': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
