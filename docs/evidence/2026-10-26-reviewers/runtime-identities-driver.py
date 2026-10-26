import json
import os
import pwd
import stat
from datetime import UTC, datetime
from pathlib import Path

os.umask(0o077)
rows = []
for number in range(1, 6):
    name = f'judge{number}'
    file = Path(f'/etc/forgeflow/instances/{name}.json')
    config = json.loads(file.read_text())
    instance = json.loads((Path(config['sandbox_root']) / 'vps-instance.json').read_text())
    api = pwd.getpwnam(config['api_user'])
    dsh = pwd.getpwnam(config['dsh_user'])
    permissions = {}
    for label, path in {'runtime_config':file, 'environment':file.with_suffix('.env'),
                        'backup_config':file.with_suffix('.backup.json'),
                        'case_directory':Path(config['case_root']),
                        'executor_socket':Path(config['socket'])}.items():
        state = path.stat()
        permissions[label] = {'uid':state.st_uid,'gid':state.st_gid,'mode':oct(stat.S_IMODE(state.st_mode))}
        if label in {'runtime_config','environment','backup_config'}:
            assert state.st_uid == 0 and stat.S_IMODE(state.st_mode) == 0o600
        if label == 'case_directory':
            assert state.st_uid == api.pw_uid and stat.S_IMODE(state.st_mode) & 0o007 == 0
        if label == 'executor_socket':
            assert stat.S_ISSOCK(state.st_mode) and stat.S_IMODE(state.st_mode) & 0o007 == 0
    assert not config['allow_initialize'] and instance['phase'] == 'ready'
    rows.append({'instance':name, 'generation':instance['generation'],'image_id':instance['image_id'],
                 'api_uid':api.pw_uid,'assistant_uid':dsh.pw_uid,'permissions':permissions,
                 'allow_initialize':False,'phase':'ready','odoo_memory_mb':config['odoo_memory_mb'],
                 'odoo_cpus':config['odoo_cpus']})
assert len({v for row in rows for v in [row['api_uid'],row['assistant_uid']]}) == 10
assert len({row['generation'] for row in rows}) == 5
Path('/root/forgeflow-reviewer-runtime-identities.json').write_text(json.dumps({
    'checked_at':datetime.now(UTC).isoformat(),'instances':rows,'distinct_unix_identities':True,
    'private_file_permissions_passed':True,'model_calls':False,'business_writes':False},indent=2)+'\n')
print(json.dumps({'runtime_identity_checks_passed':True,'instances':5}))
