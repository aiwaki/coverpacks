"""Explicit local preparation, personal login and resumable Telegram publishing."""
from __future__ import annotations

import asyncio
import base64
import binascii
import getpass
import hashlib
import html
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import warnings

from telethon import TelegramClient, connection, errors

from .emoji import prepare
from .state import State, collection_lock
from .telegram_transport import TelegramTransport, TelegramResultError

PRIVATE_DIR = Path.home() / 'Library' / 'Application Support' / 'coverpacks'
DEFAULT_PREFIX = 'sleeve_vinyl'
CHANNEL_TITLE = 'SLEEVE · обложки на виниле'
CHANNEL_ABOUT = ('Обложки виниловых релизов в Telegram emoji.\n'
    'Нумерованные emoji-паки. Первоисточники — в каждом выпуске.\n'
    'SLEEVE — музыка, которую видно.')


def add_commands(sub):
    for name in ('emoji-prepare','telegram-plan','telegram-auth','telegram-publish','telegram-status'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--data-dir',type=Path,default=Path('data'))
        if name == 'emoji-prepare':
            cmd.add_argument('--task',type=int)
            cmd.add_argument('--pack-size',type=int,default=200)
            cmd.add_argument('--prefix',default=DEFAULT_PREFIX)
            cmd.add_argument('--brand',default='SLEEVE')
        if name in {'telegram-plan','telegram-publish'}:
            cmd.add_argument('--manifest',type=Path)
            cmd.add_argument('--channel-config',type=Path)
        if name in {'telegram-auth','telegram-publish'}:
            cmd.add_argument('--credentials',type=Path,default=PRIVATE_DIR / 'telegram.json')
            cmd.add_argument('--session',type=Path,default=PRIVATE_DIR / 'telegram.session')
        if name == 'telegram-auth':
            cmd.add_argument('--configure',action='store_true',help='Локально ввести API ID/hash без вывода в терминал')
            cmd.add_argument('--phone',help='Номер для первого входа; лучше ввести в скрытом запросе')
            cmd.add_argument('--password-only',action='store_true',
                help='Продолжить двухэтапную проверку после принятого кода, без повторного запроса кода')


def read_json(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):
        raise ValueError(f'Ожидался JSON-объект: {path}')
    return value


def atomic_json(path,value,private=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    if private:
        path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix+'.part')
    fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600 if private else 0o644)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:
            json.dump(value,handle,ensure_ascii=False,indent=2)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        if private:
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def private_config(path, *, required=False):
    if not path.is_file():
        if required:
            raise ValueError('Telegram ещё не подключён. Выполните coverpacks telegram-auth --configure; API ID/hash вводятся локально и не нужны в чате.')
        return {}
    if path.stat().st_mode & 0o077:
        raise ValueError(f'Файл ключей доступен другим пользователям. Выполните chmod 600 "{path}".')
    try:
        return read_json(path)
    except (ValueError,UnicodeError):
        raise ValueError('Закрытая конфигурация Telegram должна содержать корректный JSON-объект.') from None


def credentials(path):
    api_id = os.environ.get('COVERPACKS_TG_API_ID')
    api_hash = os.environ.get('COVERPACKS_TG_API_HASH')
    if not api_id and not api_hash:
        data = private_config(path,required=True)
        api_id,api_hash = data.get('api_id'),data.get('api_hash')
    try:
        number = int(api_id)
    except (TypeError,ValueError):
        raise ValueError('В локальной конфигурации Telegram отсутствует корректный api_id') from None
    if number < 1 or not isinstance(api_hash,str) or len(api_hash)!=32 or any(c not in '0123456789abcdefABCDEF' for c in api_hash):
        raise ValueError('В локальной конфигурации Telegram некорректный API ID/hash')
    return number,api_hash


def proxy_options(path):
    """Translate an optional private JSON proxy object into Telethon arguments.

    proxy: {type: mtproxy, host, port, secret} accepts a 16-byte secret or a
    dd-prefixed secret in hex/base64. Real ee/fake-TLS is unsupported by Telethon
    1.45 and must not be silently downgraded by its normalize_secret routine.
    proxy: {type: socks5, host, port, username?, password?, rdns?} uses the optional
    python-socks[asyncio] dependency. Missing/null proxy means a direct connection.
    """
    proxy = private_config(path).get('proxy')
    if proxy is None:
        return {}
    if not isinstance(proxy,dict):
        raise ValueError('proxy должен быть JSON-объектом или null в закрытой конфигурации Telegram.')
    kind = proxy.get('type')
    if not isinstance(kind,str) or kind not in {'mtproxy','socks5'}:
        raise ValueError('Тип proxy должен быть mtproxy или socks5.')
    allowed = ({'type','host','port','secret'} if kind=='mtproxy'
               else {'type','host','port','username','password','rdns'})
    if set(proxy)-allowed:
        raise ValueError('Неизвестные поля proxy в закрытой конфигурации Telegram.')
    host,port = proxy.get('host'),proxy.get('port')
    if (not isinstance(host,str) or not host or len(host)>253
            or any(c.isspace() or ord(c)<32 or c in '/@?#' for c in host)):
        raise ValueError('В proxy нужен корректный host без URL и учётных данных.')
    if type(port) is not int or not 1<=port<=65535:
        raise ValueError('В proxy нужен целочисленный port от 1 до 65535.')
    if kind=='mtproxy':
        secret = proxy.get('secret')
        if not isinstance(secret,str) or not secret or len(secret)>2048:
            raise ValueError('В MTProxy нужен корректный secret в hex или base64.')
        try:
            raw = bytes.fromhex(secret)
        except ValueError:
            try:
                raw = base64.b64decode(secret+'='*(-len(secret)%4),altchars=b'-_',validate=True)
            except (ValueError,binascii.Error):
                raise ValueError('В MTProxy нужен корректный secret в hex или base64.') from None
        if len(raw)>16 and raw[0]==0xee:
            raise ValueError('Telethon 1.45 не поддерживает MTProxy ee/fake-TLS. Нужен обычный/dd MTProxy или SOCKS5; secret автоматически не изменяется.')
        if not (len(raw)==16 or (len(raw)==17 and raw[0]==0xdd)):
            raise ValueError('MTProxy secret должен содержать 16 байт либо префикс dd и 16 байт.')
        normalized = raw.hex()
        # Telethon strips textual ee/dd even from a valid unprefixed 16-byte key.
        # Base64 avoids that ambiguity without changing any key bytes.
        if len(raw)==16 and raw[0] in {0xdd,0xee}:
            normalized = base64.b64encode(raw).decode('ascii')
        return {'connection':connection.ConnectionTcpMTProxyRandomizedIntermediate,
                'proxy':(host,port,normalized)}
    for field in ('username','password'):
        if proxy.get(field) is not None and not isinstance(proxy[field],str):
            raise ValueError('SOCKS5 username/password должны быть строками.')
    if type(proxy.get('rdns',True)) is not bool:
        raise ValueError('SOCKS5 rdns должен быть true или false.')
    if importlib.util.find_spec('python_socks') is None:
        raise ValueError('Для SOCKS5 установите python-socks[asyncio] в окружение coverpacks.')
    return {'proxy':{'proxy_type':'socks5','addr':host,'port':port,
                     'username':proxy.get('username'),'password':proxy.get('password'),
                     'rdns':proxy.get('rdns',True)}}


def client_for(args):
    api_id,api_hash = credentials(args.credentials)
    network_options = proxy_options(args.credentials)
    args.session.parent.mkdir(parents=True,exist_ok=True)
    args.session.parent.chmod(0o700)
    old_umask = os.umask(0o077)
    try:
        # Telethon's low-level diagnostic logs may contain request bodies or
        # proxy exceptions. The CLI reports typed, redacted failures instead.
        logger = logging.getLogger('coverpacks.telegram.private')
        logger.setLevel(logging.CRITICAL+1)
        logger.propagate = False
        with warnings.catch_warnings():
            if network_options.get('connection') is connection.ConnectionTcpMTProxyRandomizedIntermediate:
                # Telethon 1.45 warns unconditionally for every proxy, although
                # TcpMTProxy connects directly to its host without python-socks.
                warnings.filterwarnings('ignore',
                    message='proxy argument will be ignored because python-socks is not installed',
                    category=UserWarning,module=r'telethon\.client\.telegrambaseclient')
            client = TelegramClient(str(args.session),api_id,api_hash,
                device_model='Coverpacks · macOS',system_version='macOS',app_version='0.1.0',
                request_retries=0,connection_retries=1,retry_delay=1,timeout=15,
                flood_sleep_threshold=0,raise_last_call_error=True,receive_updates=False,
                base_logger=logger,**network_options)
    finally:
        os.umask(old_umask)
    if args.session.exists():
        args.session.chmod(0o600)
    return client


async def authorize(args):
    password_only = getattr(args,'password_only',False)
    if password_only and args.configure:
        raise ValueError('--password-only продолжает существующий вход и не используется с --configure.')
    if password_only and not args.session.is_file():
        raise ValueError('Для --password-only нужна существующая локальная сессия после принятого кода Telegram.')
    if args.configure:
        if not sys.stdin.isatty():
            raise ValueError('Для ввода ключей нужен интерактивный локальный терминал.')
        api_id = getpass.getpass('Telegram API ID: ').strip()
        api_hash = getpass.getpass('Telegram API hash: ').strip()
        try:
            api_id = int(api_id)
        except ValueError:
            raise ValueError('API ID должен быть числом') from None
        if api_id<1 or len(api_hash)!=32 or any(c not in '0123456789abcdefABCDEF' for c in api_hash):
            raise ValueError('Некорректный API ID/hash')
        configuration = private_config(args.credentials)
        configuration.update({'api_id':api_id,'api_hash':api_hash})
        atomic_json(args.credentials,configuration,private=True)
    client = client_for(args)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            if not sys.stdin.isatty():
                raise ValueError('Сессия не авторизована. Запустите telegram-auth в локальном терминале.')
            if password_only:
                password = getpass.getpass('Пароль двухэтапной проверки Telegram (не сохраняется): ')
                try:
                    await client.sign_in(password=password)
                finally:
                    del password
            else:
                phone = args.phone or getpass.getpass('Номер Telegram в международном формате: ').strip()
                sent = await client.send_code_request(phone)
                code = getpass.getpass('Код из служебного чата Telegram: ').strip()
                try:
                    await client.sign_in(phone=phone,code=code,phone_code_hash=sent.phone_code_hash)
                except errors.SessionPasswordNeededError:
                    password = getpass.getpass('Пароль двухэтапной проверки Telegram (не сохраняется): ')
                    try:
                        await client.sign_in(password=password)
                    finally:
                        del password
                finally:
                    del code,phone
        me = await client.get_me()
        if me.bot:
            raise ValueError('Нужен личный аккаунт Telegram, а не бот')
        print(f"Telegram подключён: @{me.username}" if me.username else 'Личная сессия Telegram подключена.')
    finally:
        await client.disconnect()
        if args.session.exists():
            args.session.chmod(0o600)


def manifest_for(args):
    if args.manifest:
        path=args.manifest.resolve()
    else:
        pointer=args.data_dir/'emoji'/DEFAULT_PREFIX/'latest.json'
        if not pointer.is_file():
            raise ValueError('Сначала выполните emoji-prepare --task ID')
        data=read_json(pointer)
        path=Path(data['manifest'])
        if not path.is_absolute():
            path=pointer.parent/path
        if hashlib.sha256(path.read_bytes()).hexdigest()!=data['sha256']:
            raise ValueError('SHA manifest не совпадает с latest.json')
    return path.resolve()


def config_for(args):
    path=args.channel_config or args.data_dir/'telegram'/'channel.json'
    if path.exists():
        return read_json(path)
    if args.channel_config:
        raise ValueError(f'Конфигурация канала отсутствует: {path}')
    config={'title':CHANNEL_TITLE,'about':CHANNEL_ABOUT,'username':DEFAULT_PREFIX,'photo_previews':True,
            'avatar':str((args.data_dir/'telegram'/'branding'/'avatar.png').resolve())}
    atomic_json(path,config)
    return config


def write_plan(args):
    from .publisher import _pack_posts, _publication_posts
    from .telegram_transport import _message_entities
    from telethon.extensions import html as telegram_html
    manifest_path=manifest_for(args)
    manifest=read_json(manifest_path)
    config=config_for(args)
    destination=args.data_dir/'telegram'
    destination.mkdir(parents=True,exist_ok=True)
    # An offline plan uses the last observed account limit, or the conservative
    # default before the first connection. The publisher refreshes it live.
    caption_limit=1024
    ledger_path=destination/'publish.sqlite3'
    if ledger_path.exists():
        import sqlite3
        with sqlite3.connect(ledger_path.resolve().as_uri()+'?mode=ro',uri=True) as ledger:
            row=ledger.execute("SELECT value FROM meta WHERE key='account_capabilities'").fetchone()
            if row:
                caption_limit=json.loads(row[0])['max_caption_length']
    body=['<!doctype html><meta charset="utf-8"><title>SLEEVE · план публикации</title>',
        '<style>body{font:16px -apple-system,sans-serif;background:#eee9dc;color:#171714;max-width:1000px;margin:50px auto;padding:24px}h1{font-size:64px;letter-spacing:-3px}h2{margin-top:40px}p{line-height:1.6}img.avatar{width:150px;border-radius:50%}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:18px}.grid img{width:100px;height:100px;image-rendering:auto}article{font-size:12px}a{color:#a33421}.note{color:#665f53}</style>',
        '<h1>SLEEVE</h1>',f'<img class="avatar" src="{html.escape(Path(config["avatar"]).as_uri())}">',
        f'<h2>{html.escape(config["title"])}</h2><p>{html.escape(config["about"]).replace(chr(10),"<br>")}</p>',
        f'<p>Планируемый адрес: t.me/{html.escape(config["username"])}<br>Фото, альбомы и ссылка на пак — вместе. Большой выпуск делится на страницы по лимиту подписи.</p>',
        '<p class="note">Это предварительный просмотр. Фактическое состояние публикации показывает telegram-status.</p>']
    announcements=[]
    for pack in manifest['packs']:
        body.append(f'<h2>{html.escape(pack["title"])}</h2><p>{len(pack["items"])} emoji · 100×100 · lossless WebP<br>t.me/addemoji/{html.escape(pack["short_name"])}</p><div class="grid">')
        for item in pack['items']:
            preview=Path(item['png_path'])
            if not preview.is_absolute():preview=manifest_path.parent/preview
            body.append(f'<article><img src="{html.escape(preview.as_uri())}"><p>{item["position"]:02d} · {html.escape(item["artist"] or "")}<br>{html.escape(item["title"] or "")}</p></article>')
        body.append('</div>')
        documents=[{'id':0,'alt':pack['emoji']} for _ in pack['items']]
        if config.get('photo_previews',False):
            posts=_publication_posts(pack,documents,True,manifest.get('brand','SLEEVE'),config['username'],destination/'previews'/pack['short_name'],caption_limit=caption_limit)
        else:
            posts=_pack_posts(pack,documents,True,manifest.get('brand','SLEEVE'))
        announcements.append({'short_name':pack['short_name'],'posts':[post['body'] for post in posts]})
        for post in posts:
            if post.get('photo_path'):
                body.append('<img style="max-width:100%;height:auto" src="'+html.escape(Path(post['photo_path']).as_uri())+'">')
            rendered=telegram_html.unparse(post['body'],_message_entities(post['body'],post['documents'],post['links'],post.get('formatting')))
            body.append('<h3>Текст анонса</h3><div style="white-space:pre-wrap;line-height:1.6">'+rendered+'</div>')
    path=destination/'publication-plan.html'; path.write_text(''.join(body),encoding='utf-8')
    report={'manifest':str(manifest_path),'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            'channel':config,'caption_limit':caption_limit,'packs':[{'title':p['title'],'short_name':p['short_name'],'count':len(p['items'])} for p in manifest['packs']], 'announcements':announcements,'preview':str(path.resolve())}
    atomic_json(destination/'publication-plan.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return path


async def publish(args):
    from .publisher import PublicationState,Publisher
    ownership = PublicationState(args.data_dir/'telegram')
    try:
        if ownership.get('cloud_authoritative'):
            raise ValueError('Публикацией управляет облако. Используйте SLEEVE daily в GitHub Actions: локальная копия журнала может быть устаревшей.')
    finally:
        ownership.close()
    manifest=manifest_for(args)
    config=config_for(args)
    # Re-run the frozen selection check against today's local catalogue before
    # connecting, so a later excluded cover cannot be silently published.
    frozen=read_json(manifest)
    state=State(args.data_dir)
    try:
        current={r['seq']:r for r in state.rows(frozen['task_id'])}
        for pack in frozen['packs']:
            for item in pack['items']:
                row=current.get(item['seq'])
                if not row or row['excluded'] or row['needs_review'] or row['status']!='downloaded' or row['sha256']!=item['source_sha256']:
                    raise ValueError(f"Обложка #{item['seq']} больше не выбрана для публикации. Замороженный manifest не отправлен.")
    finally:
        state.close()
    client=client_for(args)
    ledger=PublicationState(args.data_dir/'telegram')
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('Сессия Telegram не авторизована. Выполните telegram-auth один раз.')
        report=await Publisher(ledger,TelegramTransport(client)).run(manifest,config)
        atomic_json(args.data_dir/'telegram'/'publication-result.json',report)
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        ledger.close()
        await client.disconnect()


def run(args):
    try:
        with collection_lock(args.data_dir):
            if args.command=='emoji-prepare':
                state=State(args.data_dir)
                try:
                    task=state.task(args.task)
                    print(prepare(state,task['id'],pack_size=args.pack_size,prefix=args.prefix,brand=args.brand))
                finally:state.close()
            elif args.command=='telegram-plan':
                write_plan(args)
            elif args.command=='telegram-auth':
                asyncio.run(authorize(args))
            elif args.command=='telegram-publish':
                asyncio.run(publish(args))
            elif args.command=='telegram-status':
                from .publisher import PublicationState
                ledger=PublicationState(args.data_dir/'telegram')
                try:print(json.dumps(ledger.report(),ensure_ascii=False,indent=2))
                finally:ledger.close()
        return 0
    except errors.FloodWaitError as exc:
        print(f'Telegram просит подождать {exc.seconds} с. Повторите команду после этого времени.',file=sys.stderr)
        return 2
    except (errors.RPCError,TelegramResultError) as exc:
        # API diagnostics may embed sensitive request parameters; report only type.
        print(f'Telegram: {type(exc).__name__}. Прогресс сохранён; повторная публикация сверяет состояние.',file=sys.stderr)
        return 2
    except (EOFError,ConnectionError,TimeoutError,errors.InvalidBufferError,
            errors.InvalidChecksumError,errors.SecurityError,errors.TypeNotFoundError) as exc:
        print(f'Соединение Telegram прервано ({type(exc).__name__}). Проверьте сеть или proxy в закрытой конфигурации Telegram. Прогресс сохранён.',file=sys.stderr)
        return 2
    except errors.AuthKeyNotFound:
        print('Telegram не распознал ключ локальной сессии. Проверьте подключение и повторите telegram-auth; файлы сессии не удалены.',file=sys.stderr)
        return 2
    except OSError as exc:
        print(f'Ошибка доступа к файлу или сети ({type(exc).__name__}). Проверьте доступ к локальным файлам и настройку proxy.',file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f'Ошибка: {exc}',file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Остановлено. Прогресс сохранён; повторите ту же команду.',file=sys.stderr)
        return 130
