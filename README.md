# Service Bus Backend

## Запуск сервера для внешнего доступа (белый IP)

Сервер предназначен для запуска на внешнем хосте и приема запросов от Android и других клиентов.

### 1) Переменные окружения (пример)

```bash
export PUBLIC_IP=37.200.79.56
export PORT=8000
export BASE_URL=http://37.200.79.56:8000
export DATABASE_URL=sqlite:///./service_bus.db
# или PostgreSQL:
# export DATABASE_URL=postgresql+psycopg2://postgres:password@127.0.0.1:5432/service_bus
export JWT_SECRET_KEY=CHANGE_ME_TO_A_LONG_RANDOM_SECRET
export ACCESS_TOKEN_EXPIRE_MINUTES=1440
```

### 2) Запуск через uvicorn

```bash
uvicorn service_bus_backend_main:app --host 0.0.0.0 --port 8000
```

### 3) Запуск через gunicorn (production)

```bash
gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 service_bus_backend_main:app
```

### 4) Что открыть в firewall/NAT

- Входящий TCP порт: **8000**
- IP сервера: ваш белый IP (например, `37.200.79.56`)

### 5) Проверка из браузера

Откройте:

```text
http://37.200.79.56:8000/health
```

Ожидаемый ответ:

```json
{
  "status": "ok",
  "base_url": "http://37.200.79.56:8000"
}
```

## Основные endpoint'ы

- Auth: `POST /auth/login`
- System: `GET /health`
- Admin users: `GET/POST /admin/users`, `PATCH /admin/users/{user_id}/permissions`, `DELETE /admin/users/{user_id}`
- Admin roles: `GET/POST /admin/roles`
- Admin logs: `GET /admin/logs`, `GET /admin/logs/errors`, `POST /admin/logs`
- Admin companies: `GET/POST /admin/companies`
- Driver: `POST /route/start`, `POST /route/finish`, `POST /location/update`
- Passenger/Customer: `GET /routes/active`, `GET /location/{user_id}`
- Requests: `POST /requests`, `GET /admin/requests`, `POST /admin/requests/{id}/approve`, `POST /admin/requests/{id}/reject`

## Auth flow

1. Выполнить `POST /auth/login` (form-urlencoded: `username`, `password`; совместимо с `login` вместо `username`).
2. В ответ получить `access_token`.
3. Передавать `Authorization: Bearer <token>` для защищенных endpoint'ов.

## Примеры curl

```bash
curl -X POST "http://37.200.79.56:8000/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin123"
```

```bash
curl "http://37.200.79.56:8000/health"
```

## Seed / миграции

- При старте автоматически создаются таблицы SQLAlchemy.
- Автоматически сидируются системные роли: `admin`, `driver`, `passenger`, `customer`.
- Если отсутствует пользователь `admin`, он создается с паролем `admin123` (смените пароль в production).
