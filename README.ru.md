# OpenChronicle — Локальная Память для AI-Агентов

[English](../README.md) | **Русский**

OpenChronicle — открытое решение для создания локальной, инспектируемой памяти
для AI-агентов. Захватывает контекст работы и сохраняет его в виде структурированной
Markdown-памяти с полнотекстовым поиском (FTS5 BM25).

Наш форк (`linux-server-port`) расширяет оригинальный macOS-проект поддержкой
Linux, headless-серверов и двухслойной гибридной памятью.

**Оригинал:** [Einsia/OpenChronicle](https://github.com/Einsia/OpenChronicle)
**Форк:** [samuraiIT/OpenChronicle](https://github.com/samuraiIT/OpenChronicle) (ветка `linux-server-port`)
**Лицензия:** MIT

---

## Что нового в форке

### Linux-порт (AT-SPI) — v20.89

Полноценная замена macOS-бэкенда (`mac-ax-watcher.swift` → `linux-atspi-watcher.py`).

- Те же события (focus, text input, mouse click, value change)
- Тот же JSONL-контракт — Python-обработчики не меняются
- AT-SPI2 + `python3-pyatspi`
- **3 точки интеграции** в 2 файлах: `watcher.py` + `ax_capture.py`
- → [Подробнее](docs/linux-port.md)

### Server-mode (headless) — v20.91

Захват событий на серверах без дисплея (SSH, headless Linux):

- `git_commit` — новые коммиты в отслеживаемом репозитории
- `agent_activity` — сессии Hermes/OpenClaw/Codex
- `deployment` — версии и релизы
- `system_metric` — CPU, память, диск (каждые 5 минут)
- `file_change` — изменения ключевых файлов
- → [Подробнее](docs/server-mode.md)

### SSE + JSON Fence Fix — v20.95

Исправление 98% сбоев редьюсера сессий:

- `_parse_sse_response()` — парсинг SSE-потоков от OmniRoute
- `_strip_json_fences()` — снятие ```json-обёрток Claude
- `_direct_chat_completion()` — прямой HTTP с `x-api-key` в обход litellm
- → [Подробнее](docs/sse-fix.md)

### Двухслойная память — v20.90

```
AGENT ──memory(action='add')──→ Durable (Hermes)
  │                               MEMORY.md, USER.md
  │                               Канон — истина в последней инстанции
  │
  └──search_files(path="ambient/")──→ Ambient (OpenChronicle)
                                       *.md + FTS5 + captures
                                       Свидетельство — лучший доступный контекст
```

Правило: **Durable canon, Ambient evidence.**

→ [Подробнее](docs/hybrid-memory.md)

### Unified MCP Server

Единый search-интерфейс через MCP (Model Context Protocol):

| Инструмент | Описание |
|---|---|
| `hermetic_search` | Поиск по durable + ambient (ripgrep + FTS5 BM25) |
| `hermetic_read` | Чтение файла памяти из любого слоя |
| `hermetic_list` | Список всех файлов памяти |
| `hermetic_context` | Последняя активность из ambient-слоя |
| `suggest_runbook_update` | V3-мост: анализ дрифта без записи |

FTS5 dot-syntax fix: `v20.94` → `v20_94` (автоматическая нормализация).

→ [Подробнее](docs/hermetic-mcp.md)

---

## Быстрый старт

### Требования

```bash
# Linux (Ubuntu 24.04)
sudo apt install at-spi2-core python3-pyatspi

# Установка
git clone git@github.com:samuraiIT/OpenChronicle.git
cd OpenChronicle
git checkout linux-server-port
bash install.sh
```

### Настройка

```bash
nano ~/.openchronicle/config.toml
```

```toml
[capture]
event_driven = true
heartbeat_minutes = 5
include_screenshot = false   # Headless сервер

[models.default]
model = "openai/your-model"
base_url = "http://your-llm-proxy:port/v1"
api_key_env = "YOUR_API_KEY_ENV"
```

### Запуск

```bash
# Desktop Linux (AT-SPI)
openchronicle start

# Headless сервер
OC_WATCH_DIR=/path/to/workspace \
  OPENCHRONICLE_AX_WATCHER=./resources/server-events-watcher.py \
  openchronicle start

# Статус
openchronicle status
openchronicle timeline list
```

---

## Архитектура

```
AT-SPI watcher / server watcher
        │
        │ JSONL events
        ▼
┌──────────────────────────────────────────┐
│              Capture Pipeline              │
│                                           │
│  S0 dedup → S1 parse/enrich → Scheduler   │
│       │                                   │
│       ▼                                   │
│  Timeline LLM (60s windows)              │
│       │                                   │
│       ▼                                   │
│  Session Reducer (5min)                   │
│       │                                   │
│       ▼                                   │
│  Classifier (30min → durable facts)       │
│                                           │
│  Output: Markdown files + SQLite FTS5     │
└──────────────────────────────────────────┘
        │
        │ MCP / hermetic_mcp.py
        ▼
   AI Agent (Claude, Codex, Hermes, etc.)
```

Три слоя сжатия перед извлечением фактов:

1. **Timeline (S1):** 1-минутные окна → LLM-нормализация с verbatim-сохранением
2. **Session Reducer (S2):** сессионный контекст → `event-YYYY-MM-DD.md`
3. **Classifier (S3):** durable facts → `user-*.md`, `project-*.md`, `tool-*.md`

## Ключевые дизайн-решения

1. **Сжатие перед классификацией** — 3 этапа редукции контекста
2. **Сессия как единица сжатия** — 3-rule cutter: idle gap (5m), soft cut (3m), timeout (2h)
3. **Supersede-семантика** — устаревшие факты зачёркиваются (`~~text~~`), новые добавляются
4. **Read-only MCP** — агенты только читают, writer pipeline пишет сам
5. **Гибридная память** — два слоя с чётким приоритетом (durable canon)

## Проекты-интеграции

Форк используется в мульти-агентной экосистеме:

- **Hermes Agent** — двухслойная память через `hermetic_mcp.py`
- **LLM-Server** — V3 bridge: FTS5-индексация runbook + анализ дрифта
- **OpenClaw** — ambient-контекст в multi-platform мессенджере
- **Codex, Cursor, Qwen Code** — unified MCP-поиск

## Документация

| Документ | Язык | Описание |
|---|---|---|
| [linux-port.md](docs/linux-port.md) | EN | Linux-порт через AT-SPI |
| [server-mode.md](docs/server-mode.md) | EN | Headless-серверный режим |
| [hybrid-memory.md](docs/hybrid-memory.md) | EN | Двухслойная модель памяти |
| [hermetic-mcp.md](docs/hermetic-mcp.md) | EN | Unified MCP-сервер |
| [sse-fix.md](docs/sse-fix.md) | EN | SSE + JSON fence fix |
| [architecture.md](docs/architecture.md) | EN | Архитектура (оригинал) |
| [config.md](docs/config.md) | EN | Конфигурация |
| [troubleshooting.md](docs/troubleshooting.md) | EN | Решение проблем |

## Вклад

```bash
git clone git@github.com:samuraiIT/OpenChronicle.git
git checkout linux-server-port
# ... изменения ...
# Проверить diff на секреты перед push:
git diff origin/linux-server-port | grep -E 'sk-|token|password|/home/|/opt/'
git push origin linux-server-port
```

## Статус проекта

- ✅ Linux-порт (AT-SPI) — desktop + headless
- ✅ Server-mode — git, agents, system capture
- ✅ SSE + JSON fence fix — 98% → 0% сбоев редьюсера
- ✅ Гибридная память — durable + ambient
- ✅ Unified MCP — единый search-интерфейс
- ⚠️ Classifier — требует модель с поддержкой tool-call roundtrips (см. [config.md](docs/config.md))
