import json
import asyncio
import base64
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from coverpacks import cli,telegram_cli
from coverpacks.state import State
from telethon import connection, errors
from telethon.network.connection.tcpmtproxy import TcpMTProxy


@pytest.fixture(autouse=True)
def no_remote(monkeypatch):
    monkeypatch.delenv('COVERPACKS_TG_API_ID',raising=False)
    monkeypatch.delenv('COVERPACKS_TG_API_HASH',raising=False)
    def forbid(*a,**kw):
        pytest.fail('A local-only command constructed a Telegram client')
    monkeypatch.setattr(telegram_cli,'TelegramClient',forbid)


def test_credentials_stay_private_and_do_not_enter_error_messages(tmp_path):
    path=tmp_path/'private'/'telegram.json'
    secret='a'*32
    telegram_cli.atomic_json(path,{'api_id':12345,'api_hash':secret},private=True)
    assert path.stat().st_mode&0o777==0o600
    assert path.parent.stat().st_mode&0o777==0o700
    assert telegram_cli.credentials(path)==(12345,secret)
    path.chmod(0o644)
    with pytest.raises(ValueError) as error:telegram_cli.credentials(path)
    assert secret not in str(error.value)
    assert 'chmod 600' in str(error.value)


def test_missing_auth_reports_local_setup_without_network(tmp_path):
    with pytest.raises(ValueError,match='telegram-auth --configure'):
        telegram_cli.credentials(tmp_path/'missing.json')


def test_local_publish_refuses_cloud_owned_stale_ledger(tmp_path):
    from coverpacks.publisher import PublicationState
    ledger = PublicationState(tmp_path/'telegram')
    ledger.put('cloud_authoritative', {'repository': 'owner/private'})
    ledger.close()
    with pytest.raises(ValueError, match='управляет облако'):
        asyncio.run(telegram_cli.publish(SimpleNamespace(data_dir=tmp_path)))


def test_env_credentials_override_file(tmp_path,monkeypatch):
    monkeypatch.setenv('COVERPACKS_TG_API_ID','12345')
    monkeypatch.setenv('COVERPACKS_TG_API_HASH','b'*32)
    assert telegram_cli.credentials(tmp_path/'missing.json')==(12345,'b'*32)


@pytest.mark.parametrize('api_id,api_hash',[(0,'a'*32),('bad','a'*32),(123,'wrong'),(123,None)])
def test_invalid_credentials_have_redacted_error(tmp_path,api_id,api_hash):
    path=tmp_path/'telegram.json'
    telegram_cli.atomic_json(path,{'api_id':api_id,'api_hash':api_hash},private=True)
    with pytest.raises(ValueError):telegram_cli.credentials(path)


def test_local_plan_is_escaped_and_does_not_connect(tmp_path,capsys):
    config=tmp_path/'channel.json'
    config.write_text(json.dumps({'title':'SLEEVE','about':'Preview','username':'sleeve_vinyl','avatar':str(tmp_path/'avatar.png'),'photo_previews':False}))
    png=tmp_path/'a.png';png.write_bytes(b'preview')
    manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'packs':[{'number':1,'emoji':'💿','title':'SLEEVE · 001','short_name':'sleeve_001','items':[{'position':1,'png_path':str(png),'artist':'A < B','title':'<script>bad</script>','source_url':'https://example.com/album/test'}]}]}))
    assert cli.main(['telegram-plan','--data-dir',str(tmp_path),'--manifest',str(manifest),'--channel-config',str(config)])==0
    report=json.loads(capsys.readouterr().out)
    assert report['packs'][0]['count']==1
    source=Path(report['preview']).read_text()
    assert '&lt;script&gt;' in source and '<script>' not in source
    assert 'Фактическое состояние публикации показывает telegram-status' in source
    assert not (tmp_path/'telegram'/'publish.sqlite3').exists()


def test_prepare_command_uses_latest_saved_task(tmp_path,monkeypatch,capsys):
    state=State(tmp_path); task=state.create_task({'source':'bandcamp'});state.close()
    calls=[]
    def prepare(state,task_id,**kwargs):
        calls.append((task_id,kwargs));return tmp_path/'manifest.json'
    monkeypatch.setattr(telegram_cli,'prepare',prepare)
    assert cli.main(['emoji-prepare','--data-dir',str(tmp_path)])==0
    assert calls==[(task['id'],{'pack_size':200,'prefix':'sleeve_vinyl','brand':'SLEEVE'})]


def test_offline_plan_reads_saved_caption_limit_with_relative_data_dir(tmp_path,monkeypatch,capsys):
    from coverpacks.publisher import PublicationState
    monkeypatch.chdir(tmp_path)
    data=Path('data');data.mkdir()
    config=data/'channel.json'
    config.write_text(json.dumps({'title':'SLEEVE','about':'Preview','username':'sleeve_vinyl',
        'avatar':str(tmp_path/'avatar.png'),'photo_previews':True}))
    manifest=data/'manifest.json'
    manifest.write_text(json.dumps({'packs':[{'number':1,'emoji':'💿','title':'SLEEVE · 001',
        'short_name':'sleeve_001','items':[]}]}))
    ledger=PublicationState(data/'telegram');ledger.put('account_capabilities',{'max_caption_length':4096});ledger.close()
    seen=[]
    def posts(*args,**kwargs):
        seen.append(kwargs['caption_limit']);return []
    monkeypatch.setattr('coverpacks.publisher._publication_posts',posts)
    args=SimpleNamespace(data_dir=data,manifest=manifest,channel_config=config)
    path=telegram_cli.write_plan(args)
    report=json.loads(capsys.readouterr().out)
    assert seen==[4096] and report['caption_limit']==4096 and path.exists()


def test_manifest_pointer_tampering_is_detected(tmp_path):
    folder=tmp_path/'emoji'/telegram_cli.DEFAULT_PREFIX;folder.mkdir(parents=True)
    (folder/'manifest.json').write_text('{}')
    (folder/'latest.json').write_text(json.dumps({'manifest':'manifest.json','sha256':'bad'}))
    with pytest.raises(ValueError,match='SHA manifest'):
        telegram_cli.manifest_for(SimpleNamespace(manifest=None,data_dir=tmp_path))


def test_excluded_manifest_item_is_rejected_before_client(tmp_path):
    import asyncio
    state=State(tmp_path);task=state.create_task({'source':'bandcamp'})
    state.stage_bandcamp_page(task['id'],[{'id':'bandcamp:album:1:2'}],'next')
    seq=state.rows()[0]['seq'];state.exclude(seq);state.close()
    manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'task_id':task['id'],'packs':[{'items':[{'seq':seq,'source_sha256':'a'*64}]}]}))
    args=SimpleNamespace(manifest=manifest,data_dir=tmp_path,channel_config=None)
    with pytest.raises(ValueError,match='больше не выбрана'):
        asyncio.run(telegram_cli.publish(args))


def proxy_file(tmp_path,proxy):
    path=tmp_path/'private'/'telegram.json'
    telegram_cli.atomic_json(path,{'api_id':12345,'api_hash':'a'*32,'proxy':proxy},private=True)
    return path


@pytest.mark.parametrize('proxy',[None])
def test_missing_or_null_proxy_uses_direct_connection(tmp_path,proxy):
    assert telegram_cli.proxy_options(tmp_path/'missing.json')=={}
    assert telegram_cli.proxy_options(proxy_file(tmp_path,proxy))=={}


@pytest.mark.parametrize('raw',[
    bytes.fromhex('ab'*16), bytes.fromhex('dd'+'ab'*16),
    bytes.fromhex('dd'+'ab'*15), bytes.fromhex('ee'+'ab'*15),
])
@pytest.mark.parametrize('encoding',['hex','base64'])
def test_mtproxy_preserves_exact_key_and_uses_randomized_transport(tmp_path,raw,encoding):
    secret=raw.hex() if encoding=='hex' else base64.urlsafe_b64encode(raw).decode().rstrip('=')
    path=proxy_file(tmp_path,{'type':'mtproxy','host':'127.0.0.1','port':1443,'secret':secret})
    options=telegram_cli.proxy_options(path)
    assert options['connection'] is connection.ConnectionTcpMTProxyRandomizedIntermediate
    assert options['proxy'][:2]==('127.0.0.1',1443)
    expected=raw[1:] if len(raw)==17 else raw
    assert TcpMTProxy.normalize_secret(options['proxy'][2])==expected


@pytest.mark.parametrize('encoding',['hex','base64'])
def test_fake_tls_secret_is_rejected_without_silent_downgrade(tmp_path,encoding):
    raw=b'\xee'+b'a'*16+b'example.org'
    secret=raw.hex() if encoding=='hex' else base64.urlsafe_b64encode(raw).decode().rstrip('=')
    path=proxy_file(tmp_path,{'type':'mtproxy','host':'proxy.example','port':443,'secret':secret})
    with pytest.raises(ValueError,match='ee/fake-TLS') as error:
        telegram_cli.proxy_options(path)
    assert secret not in str(error.value)
    assert json.loads(path.read_text())['proxy']['secret']==secret


@pytest.mark.parametrize('changes',[
    {'type':[]}, {'type':'http'}, {'host':'user:secret@proxy.example'},
    {'host':'https://proxy.example'}, {'host':'bad\nhost'}, {'port':True}, {'port':65536},
    {'port':'1443'}, {'secret':'not a secret!'}, {'secret':'aa'*15},
    {'unexpected':'sensitive'},
])
def test_bad_proxy_configuration_is_rejected_without_secrets(tmp_path,changes):
    proxy={'type':'mtproxy','host':'proxy.example','port':1443,'secret':'ab'*16}|changes
    path=proxy_file(tmp_path,proxy)
    with pytest.raises(ValueError) as error:
        telegram_cli.proxy_options(path)
    assert 'sensitive' not in str(error.value)
    assert 'user:secret' not in str(error.value)
    assert 'not a secret!' not in str(error.value)


def test_socks5_requires_optional_dependency_and_then_maps_fields(tmp_path,monkeypatch):
    path=proxy_file(tmp_path,{'type':'socks5','host':'127.0.0.1','port':1080,
                             'username':'private-user','password':'private-password','rdns':False})
    monkeypatch.setattr(telegram_cli.importlib.util,'find_spec',lambda name:None)
    with pytest.raises(ValueError,match='python-socks'):
        telegram_cli.proxy_options(path)
    monkeypatch.setattr(telegram_cli.importlib.util,'find_spec',lambda name:object())
    assert telegram_cli.proxy_options(path)=={'proxy':{'proxy_type':'socks5','addr':'127.0.0.1',
        'port':1080,'username':'private-user','password':'private-password','rdns':False}}


def test_client_passes_private_mtproxy_with_env_key_override_and_quiet_logs(tmp_path,monkeypatch):
    path=proxy_file(tmp_path,{'type':'mtproxy','host':'127.0.0.1','port':1443,'secret':'ab'*16})
    monkeypatch.setenv('COVERPACKS_TG_API_ID','23456')
    monkeypatch.setenv('COVERPACKS_TG_API_HASH','b'*32)
    calls=[]
    def fake_client(*args,**kwargs):
        calls.append((args,kwargs)); return object()
    monkeypatch.setattr(telegram_cli,'TelegramClient',fake_client)
    args=SimpleNamespace(credentials=path,session=tmp_path/'session-dir'/'telegram.session')
    telegram_cli.client_for(args)
    arguments,options=calls[0]
    assert arguments[1:]==(23456,'b'*32)
    assert options['proxy']==('127.0.0.1',1443,'ab'*16)
    assert options['connection'] is connection.ConnectionTcpMTProxyRandomizedIntermediate
    assert not options['base_logger'].isEnabledFor(logging.ERROR)
    assert options['base_logger'].propagate is False
    assert args.session.parent.stat().st_mode&0o777==0o700


def test_mtproxy_false_missing_socks_warning_is_filtered_only_for_known_module(tmp_path,monkeypatch):
    path=proxy_file(tmp_path,{'type':'mtproxy','host':'127.0.0.1','port':1443,'secret':'ab'*16})
    def fake_client(*args,**kwargs):
        telegram_cli.warnings.warn_explicit(
            'proxy argument will be ignored because python-socks is not installed',
            UserWarning,'telegrambaseclient.py',349,module='telethon.client.telegrambaseclient')
        telegram_cli.warnings.warn('unrelated warning',UserWarning)
        return object()
    monkeypatch.setattr(telegram_cli,'TelegramClient',fake_client)
    args=SimpleNamespace(credentials=path,session=tmp_path/'session-dir'/'telegram.session')
    with pytest.warns(UserWarning,match='unrelated warning') as warning_records:
        telegram_cli.client_for(args)
    assert len(warning_records)==1


@pytest.mark.parametrize('failure',[
    asyncio.IncompleteReadError(partial=b'private-secret',expected=30),
    ConnectionError('private-secret'), TimeoutError('private-secret'),
    OSError('private-secret'), errors.SecurityError('private-secret'),
])
def test_network_errors_are_redacted_and_return_without_traceback(tmp_path,monkeypatch,capsys,failure):
    async def fail(args):
        raise failure
    monkeypatch.setattr(telegram_cli,'authorize',fail)
    args=SimpleNamespace(command='telegram-auth',data_dir=tmp_path)
    assert telegram_cli.run(args)==2
    output=capsys.readouterr()
    assert not output.out
    assert 'private-secret' not in output.err and 'Traceback' not in output.err
    assert type(failure).__name__ in output.err


def test_reconfiguring_keys_preserves_proxy(tmp_path,monkeypatch):
    proxy={'type':'mtproxy','host':'127.0.0.1','port':1443,'secret':'ab'*16}
    path=proxy_file(tmp_path,proxy)
    answers=iter(['23456','b'*32])
    monkeypatch.setattr(telegram_cli.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(telegram_cli.getpass,'getpass',lambda prompt:next(answers))
    class Client:
        async def connect(self):pass
        async def disconnect(self):pass
        async def is_user_authorized(self):return True
        async def get_me(self):return SimpleNamespace(bot=False,username=None)
    monkeypatch.setattr(telegram_cli,'client_for',lambda args:Client())
    asyncio.run(telegram_cli.authorize(SimpleNamespace(configure=True,credentials=path,
        session=tmp_path/'telegram.session',phone=None)))
    assert telegram_cli.private_config(path)=={'api_id':23456,'api_hash':'b'*32,'proxy':proxy}


def test_password_only_requires_existing_session_before_client(tmp_path,monkeypatch):
    args=SimpleNamespace(password_only=True,configure=False,session=tmp_path/'missing.session')
    with pytest.raises(ValueError,match='существующая локальная сессия'):
        asyncio.run(telegram_cli.authorize(args))


def test_password_only_rejects_reconfiguration_before_client(tmp_path):
    args=SimpleNamespace(password_only=True,configure=True,session=tmp_path/'existing.session')
    with pytest.raises(ValueError,match='--configure'):
        asyncio.run(telegram_cli.authorize(args))


@pytest.mark.parametrize('authorized',[False,True])
def test_password_only_continues_same_session_without_phone_or_code(tmp_path,monkeypatch,capsys,authorized):
    session=tmp_path/'telegram.session';session.write_bytes(b'existing auth key placeholder')
    calls=[]
    class Client:
        async def connect(self):calls.append('connect')
        async def disconnect(self):calls.append('disconnect')
        async def is_user_authorized(self):return authorized
        async def sign_in(self,**kwargs):
            assert kwargs=={'password':'private-password'}
            calls.append('password')
        async def get_me(self):return SimpleNamespace(bot=False,username='owner')
        async def send_code_request(self,*args):pytest.fail('Must not request another OTP')
    monkeypatch.setattr(telegram_cli,'client_for',lambda args:Client())
    monkeypatch.setattr(telegram_cli.sys.stdin,'isatty',lambda:True)
    prompts=[]
    def prompt(text):
        prompts.append(text);return 'private-password'
    monkeypatch.setattr(telegram_cli.getpass,'getpass',prompt)
    args=SimpleNamespace(password_only=True,configure=False,session=session)
    asyncio.run(telegram_cli.authorize(args))
    assert calls==(['connect','disconnect'] if authorized else ['connect','password','disconnect'])
    assert len(prompts)==(0 if authorized else 1)
    assert session.read_bytes()==b'existing auth key placeholder'
    assert session.stat().st_mode&0o777==0o600
    assert 'private-password' not in capsys.readouterr().out


def test_password_only_is_exposed_by_cli_parser(tmp_path,monkeypatch):
    calls=[]
    async def authorize(args):calls.append(args.password_only)
    monkeypatch.setattr(telegram_cli,'authorize',authorize)
    assert cli.main(['telegram-auth','--password-only','--data-dir',str(tmp_path)])==0
    assert calls==[True]
