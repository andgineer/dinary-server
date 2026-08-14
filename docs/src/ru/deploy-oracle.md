# Развёртывание на Oracle Cloud Free Tier

Oracle Cloud Always Free предоставляет бессрочные VM — достаточно для работы dinary бесплатно.

## Стоимость

| Ресурс | Бесплатный лимит | Стоимость |
|--------|-----------------|-----------|
| AMD Micro VM | 2 инстанса, 1 OCPU + 1 ГБ RAM каждый | $0 навсегда |
| ARM Ampere A1 VM | До 4 OCPU, 24 ГБ RAM (общий пул — часто недоступен) | $0 навсегда |
| Загрузочный диск | 200 ГБ всего | $0 |
| Исходящий трафик | 10 ТБ/месяц | $0 |
| **Итого** | | **$0/месяц** |

!!! tip "Какой shape выбрать"
    **AMD Micro** (`VM.Standard.E2.1.Micro`, 1 ГБ RAM) — рекомендуется. Почти всегда доступен, так как Oracle резервирует отдельный пул для бесплатных аккаунтов. 1 ГБ RAM достаточно для FastAPI без Docker.

    **ARM Ampere A1** (`VM.Standard.A1.Flex`, до 24 ГБ RAM) — мощнее, но часто недоступен ("Out of host capacity"). Если удалось создать — отлично, если нет — берите AMD Micro.

!!! warning
    Oracle может отключить неактивные Always Free инстансы. Работающий сервер dinary поддерживает инстанс активным. Если инстанс отключён, VM можно пересоздать, но runtime source of truth лежит в `data/dinary.db` на диске, а не в Google Sheets. Перед деструктивными операциями делайте backup `~/dinary/data/` и не считайте sheet logging таблицу полноценным источником восстановления.

## Требования

- JSON-ключ сервисного аккаунта Google в `~/.config/gspread/service_account.json` — см. [Настройка Google Sheets](google-sheets-setup.md).
- Пара SSH-ключей для подключения к VM.

## 1. Создание аккаунта

1. Перейдите на [cloud.oracle.com](https://cloud.oracle.com/) → **Sign Up**.
2. Выберите регион (нельзя изменить позже).
3. Завершите верификацию (нужна банковская карта, но списания не будет для Always Free ресурсов).

!!! tip "Выбор региона"
    Регион выбирается навсегда. Доступность ARM-инстансов зависит от региона. По отзывам, **Ashburn**, **Phoenix**, **Frankfurt** и **London** чаще имеют свободные ARM-мощности. AMD Micro доступен во всех регионах.

!!! note
    Подтверждение аккаунта может занять от нескольких часов до нескольких дней.

## 2. Настройка сети (VCN)

Для VM нужна Virtual Cloud Network (VCN) с public subnet и internet gateway. Создайте их **до** создания VM:

1. Перейдите в **Networking** → **Virtual Cloud Networks** → **Create VCN**.
      - **Name**: `dinary-vcn` (или любое имя)
      - **IPv4 CIDR Blocks**: `10.0.0.0/16`
      - Нажмите **Create VCN**.

2. Внутри созданной VCN → **Subnets** → **Create Subnet**.
      - **Name**: `public-subnet`
      - **Subnet type**: Regional
      - **IPv4 CIDR Block**: `10.0.0.0/24`
      - **Subnet access**: **Public Subnet**
      - Нажмите **Create Subnet**.

3. Внутри VCN → **Internet Gateways** → **Create Internet Gateway**.
      - **Name**: `internet-gw`
      - Нажмите **Create Internet Gateway**.

4. Внутри VCN → **Route Tables** → нажмите на дефолтную route table → **Add Route Rules**.
      - **Destination CIDR Block**: `0.0.0.0/0`
      - **Target Type**: Internet Gateway
      - **Target**: выберите `internet-gw`
      - Нажмите **Add Route Rules**.

## 3. Создание VM

1. Перейдите в **Compute** → **Instances** → **Create Instance**.
2. Настройте:
      - **Image**: `Canonical Ubuntu 22.04 Minimal` (для AMD Micro — без `aarch64` в названии)
      - **Shape**: `VM.Standard.E2.1.Micro` — 1 OCPU, 1 ГБ RAM
      - **Capacity**: `On-demand capacity`
      - **Availability / Live migration**: `Let Oracle Cloud Infrastructure choose the best migration option`
      - **Networking**: выберите `dinary-vcn` → выберите `public-subnet` → поставьте галку **Automatically assign public IPv4 address**
      - **SSH keys**: загрузите ваш публичный ключ
      - **Cloud-init script**: оставьте пустым
3. Нажмите **Create**.

!!! tip "Альтернатива: ARM"
    Если ARM-мощности доступны, можно выбрать `Canonical Ubuntu 22.04 Minimal aarch64` + shape `VM.Standard.A1.Flex` (1 OCPU, 6 ГБ RAM). Больше RAM позволяет использовать Docker при желании. Остальная настройка такая же.

## 4. Настройка .deploy/.env

Вся инстанс-специфичная конфигурация (что не коммитится в репо) живёт в директории `.deploy/` в корне репозитория. После создания VM скопируйте публичный IP из панели Oracle и создайте `.deploy/.env` на ноутбуке:

```bash
mkdir -p .deploy
cp .deploy.example/.env .deploy/.env
```

Отредактируйте `.deploy/.env`:

```
DINARY_DEPLOY_HOST=ubuntu@<PUBLIC_IP>
# DINARY_TUNNEL=tailscale  # tailscale (по умолч.) | cloudflare | none
# DINARY_SHEET_LOGGING_SPREADSHEET=https://docs.google.com/spreadsheets/d/YOUR_ID/edit
```

`inv setup-server` синхронизирует локальные `.deploy/.env` на VM в
`/home/ubuntu/dinary/.deploy/`. Каталог категорий заполняется
автоматически при первом запуске.

Проверьте SSH-доступ:

```bash
ssh ubuntu@<PUBLIC_IP>
```

## 5. Настройка сервера

На вашем ноутбуке, в репозитории dinary:

```bash
inv setup-server
```

Эта единственная команда выполняет всё на VM через SSH:

- Устанавливает системные пакеты (python3, git)
- Устанавливает uv (менеджер пакетов Python)
- Клонирует репозиторий и устанавливает зависимости
- Синхронизирует ваш локальный `.deploy/.env` на VM
- Загружает `~/.config/gspread/service_account.json` на VM
- Создаёт и запускает systemd-сервис `dinary`
- Настраивает туннель (Tailscale по умолчанию, или Cloudflare — в зависимости от `DINARY_TUNNEL`)

### Tailscale (по умолчанию)

Во время настройки `tailscale up` выведет URL — откройте его в браузере для входа (создайте бесплатный аккаунт если нет).

Включите HTTPS в [админ-консоли](https://login.tailscale.com/admin/dns):

1. Включите **MagicDNS** (если ещё не включён).
2. Включите **HTTPS** для вашего tailnet.

Приложение будет доступно по адресу `https://<имя-машины>.<tailnet>.ts.net` — только внутри tailnet, Tailscale должен быть запущен на устройстве.

!!! warning "Первый запуск: подождите до 10 минут"
    При первом запуске Tailscale создаёт TLS-сертификат и распространяет DNS. URL может возвращать `ERR_SSL_PROTOCOL_ERROR` несколько минут. Подождите и повторите.

### Cloudflare

Установите `DINARY_TUNNEL=cloudflare` в `.deploy/.env` перед запуском `inv setup-server`. Во время настройки `cloudflared tunnel login` попросит авторизоваться в браузере. Требуется домен, управляемый через Cloudflare DNS — см. [Cloudflare Tunnel и Access](cloudflare-setup.md).

### Без туннеля

Установите `DINARY_TUNNEL=none` чтобы пропустить настройку туннеля. Нужно открыть порты вручную:

**Security List VCN**: добавьте правило входящего трафика — Source `0.0.0.0/0`, Protocol TCP, Port `8000`.

**Файрвол ОС**:

```bash
ssh ubuntu@<PUBLIC_IP> 'sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT && sudo netfilter-persistent save'
```

## Обслуживание

| Команда | Что делает |
|---------|-----------|
| `inv deploy --ref=main` | Переключиться на ref, синхронизировать зависимости, применить миграции, перезапустить |
| `inv status --prod` | Показать статус сервисов dinary и туннеля |
| `inv logs --prod` | Показать логи dinary в реальном времени |
| `inv setup-server` | Полная настройка (безопасно запускать повторно) |
