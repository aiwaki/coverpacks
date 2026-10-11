# coverpacks

Локальный каталог альбомных обложек из Bandcamp Discover, MusicBrainz и Cover Art Archive. Сохраняет изображения, их происхождение и очередь загрузки в SQLite; строит галерею, которая открывается без сервера. Отдельные команды готовят Telegram emoji и публикуют паки в собственном канале.

Python 3.12+, macOS или Linux. Зависимости зафиксированы в `uv.lock`. Для работы с изображениями используется Pillow, для Telegram — Telethon.

## Быстрый старт

Установите [uv](https://docs.astral.sh/uv/getting-started/installation/), затем:

```sh
git clone https://github.com/aiwaki/coverpacks.git
cd coverpacks
uv sync --frozen
```

Укажите собственный публичный контакт для User-Agent. Замените `YOUR_USERNAME` своим логином; email не требуется.

```sh
export COVERPACKS_CONTACT='https://github.com/YOUR_USERNAME'
uv run coverpacks collect --source bandcamp --limit 25
uv run coverpacks status
```

Откройте `data/gallery/index.html` в браузере. На macOS:

```sh
open data/gallery/index.html
```

Для сбора публичных обложек не нужны аккаунт Bandcamp, Telegram или API-ключи. Команда `collect` ничего не публикует.

Контакт можно сохранить в `data/settings.json` вместо переменной окружения:

```json
{
  "contact": "https://github.com/YOUR_USERNAME"
}
```

## Как устроена коллекция

- Исходные скачанные файлы сохраняются без перекодирования. Превью создаются отдельно; обрезка, растяжение и AI-обработка обложек не применяются.
- SQLite хранит стабильные номера, состояние очереди, URL источников и SHA-256. Загрузку можно продолжить после остановки.
- Точные дубли изображений учитываются по SHA-256. При подготовке новых emoji-паков также сравниваются исполнитель и название альбома после нормализации Unicode, регистра и пробелов.
- Маленькие или заметно неквадратные обложки попадают в `needs_review`. Исключение записи не удаляет её файл и не меняет номера остальных записей.
- Галерея и JSONL-экспорт создаются локально в `data/`. Этот каталог не включён в Git.

```sh
uv run coverpacks collect --limit 100
uv run coverpacks retry-failed
uv run coverpacks exclude 12 48
uv run coverpacks include 12
uv run coverpacks gallery
uv run coverpacks export
```

`--limit` задаёт общий целевой размер задачи, а не количество новых файлов. Повтор с тем же лимитом продолжает очередь. По умолчанию лимит учитывает уникальные скачанные SHA-256, включая обложки, требующие просмотра, и исключённые записи; число обложек, пригодных для пака, может быть меньше.

## Источники

Bandcamp использует выборку Discover для винила и ссылку на основную обложку со страницы альбома. Это внутренний, недокументированный интерфейс сайта: его изменения могут потребовать обновления парсера. При HTTP 429 сбор останавливает проход, сохраняет очередь и учитывает `Retry-After`.

Для MusicBrainz и Cover Art Archive доступны поиск виниловых изданий, сбор альбомов, выбор исполнителя по MBID и импорт списка релизов:

```sh
uv run coverpacks collect --source musicbrainz --mode vinyl --limit 25
uv run coverpacks collect --source musicbrainz --mode albums --limit 25
uv run coverpacks collect --source musicbrainz --mode vinyl --artist-mbid ARTIST_MBID --limit 25
uv run coverpacks collect --source musicbrainz --mode vinyl --import-file albums.txt --limit 25
```

Качество зависит от источника. Проверки размера и пропорций не заменяют просмотр изображения и не гарантируют полноту каталога.

## Telegram — отдельный этап

Локальная подготовка создаёт PNG и lossless WebP 100×100, сохраняя всю обложку с прозрачными полями при необходимости. По умолчанию новые паки содержат до 200 emoji; уже подготовленные паки не перестраиваются при добавлении обложек.

Выберите собственный префикс для имён паков:

```sh
uv run coverpacks emoji-prepare --prefix your_channel --brand COVERS
```

Команда выведет путь к manifest. Используйте его вместо `MANIFEST_PATH` в следующих командах. Сохраните собственные настройки канала в `data/telegram/channel.json`; `avatar` должен указывать на существующее изображение по абсолютному пути:

```json
{
  "title": "My cover collection",
  "about": "Album covers and their sources.",
  "username": "your_channel",
  "avatar": "/ABSOLUTE/PATH/avatar.png",
  "welcome_enabled": false,
  "photo_previews": true
}
```

Сначала посмотрите локальный план:

```sh
uv run coverpacks telegram-plan --manifest MANIFEST_PATH
```

Для публикации нужны собственные API ID/hash с [my.telegram.org](https://my.telegram.org/apps) и личный аккаунт Telegram. Ввод ключей, кода и пароля происходит в локальном терминале:

```sh
uv run coverpacks telegram-auth --configure
uv run coverpacks telegram-publish --manifest MANIFEST_PATH
uv run coverpacks telegram-status
```

`telegram-publish` создаёт канал и паки и отправляет сообщения. Журнал фиксирует намерения до внешней записи и проверяет существующие результаты при повторном запуске. Фото-превью собирается из исходных обложек; полные названия альбомов и ссылки находятся в подписи к тому же фото. Большой выпуск делится по лимиту подписи аккаунта и не более чем по 50 обложек на изображение.

Telegram может перекодировать emoji; локальный lossless WebP не означает сохранение точных пикселей на сервере. Сессия Telethon даёт доступ к личному аккаунту. Файлы сессии, API-конфигурацию, базу публикаций и архивы данных нельзя добавлять в публичный репозиторий.

В исходниках есть модули ежедневного запуска и облачного хранения. Они требуют отдельного приватного хранилища и настройки; рабочее расписание публикаций и данные оператора в эту копию не включены. Публичный GitHub Actions workflow запускает только офлайн-тесты.

## Разработка

```sh
uv run pytest -q
uv run coverpacks --help
```

Тесты используют поддельные сетевые ответы и локально созданные изображения. Они не требуют ключей и не подтверждают доступность внешних сервисов.

## Лицензия и изображения

Лицензия исходного кода пока не выбрана. Обложки, Telegram-сессии и рабочие архивы в репозиторий не включены. Публикация кода не передаёт права на изображения из внешних источников.
