from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Generator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    desc,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

# =========================
# CONFIG
# =========================
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./service_bus.db",
)
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_TO_A_LONG_RANDOM_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 24)))
SERVER_PORT = int(os.getenv("PORT", "8000"))
PUBLIC_IP = os.getenv("PUBLIC_IP", "37.200.79.56")
BASE_URL = os.getenv("BASE_URL", f"http://{PUBLIC_IP}:{SERVER_PORT}")
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(1024 * 1024)))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "120"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
logger = logging.getLogger("service_bus")
if not logger.handlers:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


# =========================
# DATABASE
# =========================
class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


ROLE_ADMIN = "admin"
ROLE_DRIVER = "driver"
ROLE_PASSENGER = "passenger"
ROLE_CUSTOMER = "customer"


class RouteStatus(str, Enum):
    active = "active"
    finished = "finished"


class LogLevel(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    success = "success"


class RequestStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class CustomerKind(str, Enum):
    person = "person"
    company = "company"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    login: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    vehicle_model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    license_plate: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_track: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_manage_users: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_view_logs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    routes: Mapped[list["ActiveRoute"]] = relationship(back_populates="driver", cascade="all, delete-orphan")
    location: Mapped[Optional["Location"]] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)
    logs: Mapped[list["SystemLog"]] = relationship(back_populates="user")
    requests: Mapped[list["BusRequest"]] = relationship(
        back_populates="requester",
        foreign_keys="BusRequest.requester_id",
    )
    processed_requests: Mapped[list["BusRequest"]] = relationship(
        back_populates="processor",
        foreign_keys="BusRequest.processed_by",
    )


class UserRoleEntry(Base):
    __tablename__ = "user_roles"

    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class BusRequest(Base):
    __tablename__ = "bus_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    requester_kind: Mapped[CustomerKind] = mapped_column(SAEnum(CustomerKind), nullable=False)
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    route_from: Mapped[str] = mapped_column(String(255), nullable=False)
    route_to: Mapped[str] = mapped_column(String(255), nullable=False)
    trip_time: Mapped[str] = mapped_column(String(100), nullable=False)
    passenger_count: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    status: Mapped[RequestStatus] = mapped_column(SAEnum(RequestStatus), nullable=False, default=RequestStatus.pending, index=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    processed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    requester: Mapped[User] = relationship(back_populates="requests", foreign_keys=[requester_id])
    processor: Mapped[Optional[User]] = relationship(back_populates="processed_requests", foreign_keys=[processed_by])


class ActiveRoute(Base):
    __tablename__ = "active_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    start_name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_lat: Mapped[float] = mapped_column(Float, nullable=False)
    start_lng: Mapped[float] = mapped_column(Float, nullable=False)
    end_name: Mapped[str] = mapped_column(String(255), nullable=False)
    end_lat: Mapped[float] = mapped_column(Float, nullable=False)
    end_lng: Mapped[float] = mapped_column(Float, nullable=False)
    start_time: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[RouteStatus] = mapped_column(
        SAEnum(RouteStatus),
        default=RouteStatus.active,
        nullable=False,
        index=True,
    )

    driver: Mapped[User] = relationship(back_populates="routes")


class Location(Base):
    __tablename__ = "locations"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user: Mapped[User] = relationship(back_populates="location")


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    level: Mapped[LogLevel] = mapped_column(SAEnum(LogLevel), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    extra_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    user: Mapped[Optional[User]] = relationship(back_populates="logs")


# =========================
# SCHEMAS
# =========================
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    login: str


class UserCreate(BaseModel):
    login: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=4, max_length=128)
    role: str = Field(..., min_length=1, max_length=100)
    vehicle_model: Optional[str] = None
    license_plate: Optional[str] = None
    is_active: bool = True
    can_track: bool = True
    can_manage_users: bool = False
    can_view_logs: bool = False


class UserPermissionsUpdate(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    can_track: Optional[bool] = None
    can_manage_users: Optional[bool] = None
    can_view_logs: Optional[bool] = None
    vehicle_model: Optional[str] = None
    license_plate: Optional[str] = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    login: str
    role: str
    vehicle_model: Optional[str]
    license_plate: Optional[str]
    is_active: bool
    can_track: bool
    can_manage_users: bool
    can_view_logs: bool


class RouteStartRequest(BaseModel):
    start_name: str
    start_lat: float
    start_lng: float
    end_name: str
    end_lat: float
    end_lng: float
    start_time: str


class RouteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    driver_id: int
    start_name: str
    start_lat: float
    start_lng: float
    end_name: str
    end_lat: float
    end_lng: float
    start_time: str
    status: RouteStatus


class ActiveRouteRead(BaseModel):
    id: int
    driver_id: int
    driver_login: str
    vehicle_model: Optional[str]
    license_plate: Optional[str]
    start_name: str
    end_name: str
    start_time: str
    status: RouteStatus


class LocationUpdateRequest(BaseModel):
    latitude: float
    longitude: float


class LocationRead(BaseModel):
    user_id: int
    latitude: float
    longitude: float
    updated_at: datetime


class LogCreate(BaseModel):
    level: LogLevel
    source: str
    message: str
    user_id: Optional[int] = None
    extra_json: Optional[dict[str, Any]] = None


class LogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    level: LogLevel
    source: str
    message: str
    user_id: Optional[int]
    extra_json: Optional[dict[str, Any]]
    created_at: datetime


class MessageResponse(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str
    base_url: str


class RoleCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=255)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    description: Optional[str]
    is_system: bool


class BusRequestCreate(BaseModel):
    requester_kind: CustomerKind
    company_name: Optional[str] = Field(default=None, max_length=255)
    route_from: str = Field(..., min_length=1, max_length=255)
    route_to: str = Field(..., min_length=1, max_length=255)
    trip_time: str = Field(..., min_length=1, max_length=100)
    passenger_count: int = Field(..., ge=1, le=10000)
    comment: Optional[str] = Field(default=None, max_length=1000)


class BusRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requester_id: int
    requester_login: str
    requester_kind: CustomerKind
    company_name: Optional[str]
    route_from: str
    route_to: str
    trip_time: str
    passenger_count: int
    comment: Optional[str]
    status: RequestStatus
    rejection_reason: Optional[str]
    processed_by: Optional[int]
    processed_by_login: Optional[str]
    created_at: datetime
    processed_at: Optional[datetime]


class BusRequestDecision(BaseModel):
    rejection_reason: Optional[str] = Field(default=None, max_length=1000)


# =========================
# UTILS
# =========================
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


REQUEST_TIMESTAMPS_BY_IP: dict[str, list[datetime]] = {}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_user_by_login(db: Session, login: str) -> Optional[User]:
    return db.scalar(select(User).where(User.login == login))


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.get(User, user_id)


def get_role_by_code(db: Session, code: str) -> Optional[UserRoleEntry]:
    return db.get(UserRoleEntry, code)


def create_log(
    db: Session,
    level: LogLevel,
    source: str,
    message: str,
    user_id: Optional[int] = None,
    extra_json: Optional[dict[str, Any]] = None,
) -> SystemLog:
    item = SystemLog(
        level=level,
        source=source,
        message=message,
        user_id=user_id,
        extra_json=extra_json,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def authenticate_user(db: Session, login: str, password: str) -> Optional[User]:
    user = get_user_by_login(db, login)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить токен",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_id(db, int(user_id))
    if user is None or not user.is_active:
        raise credentials_exception
    return user


def require_role(*roles: str):
    def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Недостаточно прав",
            )
        return current_user

    return checker


def require_log_access(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role == ROLE_ADMIN or current_user.can_view_logs:
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Недостаточно прав для просмотра логов",
    )


# =========================
# APP
# =========================
@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        default_roles = [
            (ROLE_ADMIN, "Администратор"),
            (ROLE_DRIVER, "Водитель"),
            (ROLE_PASSENGER, "Пассажир"),
            (ROLE_CUSTOMER, "Заказчик"),
        ]
        for code, description in default_roles:
            if not get_role_by_code(db, code):
                db.add(UserRoleEntry(code=code, description=description, is_system=True))
        db.commit()

        admin = get_user_by_login(db, "admin")
        if not admin:
            db.add(
                User(
                    login="admin",
                    password_hash=hash_password("admin123"),
                    role=ROLE_ADMIN,
                    is_active=True,
                    can_track=True,
                    can_manage_users=True,
                    can_view_logs=True,
                )
            )
            db.commit()
            create_log(db, LogLevel.success, "startup", "Создан стандартный администратор admin")

    yield


app = FastAPI(
    title="Служебный Автобус API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_tags=[
        {"name": "Авторизация", "description": "Вход в систему и получение JWT токена"},
        {"name": "Администрирование", "description": "Управление пользователями и правами"},
        {"name": "Логи", "description": "Просмотр системных логов, ошибок и предупреждений"},
        {"name": "Водитель", "description": "Управление рейсами и геопозицией"},
        {"name": "Пассажир", "description": "Просмотр активных автобусов и координат"},
        {"name": "Система", "description": "Служебные эндпоинты проверки состояния"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(exc.detail), "code": "HTTP_ERROR"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    client_ip = request.client.host if request.client else "unknown"
    logger.exception("Unhandled exception from %s on %s", client_ip, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "Внутренняя ошибка сервера", "code": "INTERNAL_SERVER_ERROR"},
    )


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui_html() -> HTMLResponse:
    html = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="Служебный Автобус API — Документация",
        swagger_favicon_url="https://fastapi.tiangolo.com/img/favicon.png",
    )

    content = html.body.decode("utf-8")
    injected_script = """
    <script>
    function replaceText() {
      const map = new Map([
        ["Available authorizations", "Доступные авторизации"],
        ["Authorize", "Войти"],
        ["Close", "Закрыть"],
        ["Try it out", "Попробовать"],
        ["Execute", "Выполнить"],
        ["Cancel", "Отмена"],
        ["Clear", "Очистить"],
        ["Responses", "Ответы"],
        ["Parameters", "Параметры"],
        ["Request body", "Тело запроса"],
        ["Example Value", "Пример значения"],
        ["Schema", "Схема"],
        ["Download", "Скачать"],
        ["Server response", "Ответ сервера"],
        ["Response body", "Тело ответа"],
        ["Response headers", "Заголовки ответа"],
        ["No parameters", "Нет параметров"],
        ["No operations defined in spec!", "В спецификации нет операций"],
        ["Type to search", "Поиск"],
        ["Username", "Логин"],
        ["Password", "Пароль"],
        ["username", "логин"],
        ["password", "пароль"],
        ["Token URL:", "URL токена:"],
        ["Flow:", "Тип потока:"],
        ["Scopes:", "Области доступа:"],
        ["Description", "Описание"]
      ]);

      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
      const nodes = [];

      while (walker.nextNode()) {
        nodes.push(walker.currentNode);
      }

      nodes.forEach(node => {
        const text = node.nodeValue?.trim();
        if (text && map.has(text)) {
          node.nodeValue = node.nodeValue.replace(text, map.get(text));
        }
      });

      document.title = "Служебный Автобус API — Документация";
    }

    const observer = new MutationObserver(() => replaceText());

    window.addEventListener("load", () => {
      replaceText();
      observer.observe(document.body, { childList: true, subtree: true });
    });
    </script>
    """

    content = content.replace("</body>", injected_script + "</body>")
    return HTMLResponse(content)


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BODY_BYTES:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"error": "Слишком большой запрос", "code": "REQUEST_TOO_LARGE"},
        )

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    ip_events = [t for t in REQUEST_TIMESTAMPS_BY_IP.get(client_ip, []) if t >= window_start]
    ip_events.append(now)
    REQUEST_TIMESTAMPS_BY_IP[client_ip] = ip_events
    if len(ip_events) > RATE_LIMIT_MAX_REQUESTS:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "Слишком много запросов", "code": "RATE_LIMITED"},
        )

    started_at = datetime.now(timezone.utc)
    try:
        response = await call_next(request)
        logger.info("HTTP %s %s from %s -> %s", request.method, request.url.path, client_ip, response.status_code)
        with SessionLocal() as db:
            create_log(
                db=db,
                level=LogLevel.info if response.status_code < 400 else LogLevel.warning,
                source="http",
                message=f"{request.method} {request.url.path} from {client_ip} -> {response.status_code}",
                extra_json={
                    "duration_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
                    "client_ip": client_ip,
                },
            )
        return response
    except Exception as exc:
        logger.exception("HTTP failure %s %s from %s", request.method, request.url.path, client_ip)
        with SessionLocal() as db:
            create_log(
                db=db,
                level=LogLevel.error,
                source="http",
                message=f"Необработанная ошибка: {type(exc).__name__}",
                extra_json={"path": request.url.path, "method": request.method, "client_ip": client_ip},
            )
        raise


# =========================
# AUTH
# =========================
@app.post(
    "/auth/login",
    response_model=TokenResponse,
    tags=["Авторизация"],
    summary="Вход в систему",
    description="Принимает логин и пароль, возвращает JWT токен и роль пользователя.",
)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        create_log(db, LogLevel.warning, "auth", f"Неудачная попытка входа: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

    access_token = create_access_token({"sub": str(user.id), "role": user.role})
    create_log(db, LogLevel.success, "auth", f"Успешный вход пользователя {user.login}", user_id=user.id)

    return TokenResponse(
        access_token=access_token,
        role=user.role,
        user_id=user.id,
        login=user.login,
    )


# =========================
# ADMIN
# =========================
@app.get(
    "/admin/users",
    response_model=list[UserRead],
    tags=["Администрирование"],
    summary="Получить список пользователей",
    description="Возвращает всех пользователей системы.",
)
def admin_list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    users = db.scalars(select(User).order_by(User.id)).all()
    return [UserRead.model_validate(user) for user in users]


@app.post(
    "/admin/users",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Администрирование"],
    summary="Создать пользователя",
    description="Создает нового водителя, пассажира или администратора.",
)
def admin_create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    existing = get_user_by_login(db, payload.login)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пользователь с таким логином уже существует",
        )

    role = payload.role.strip().lower()
    if not get_role_by_code(db, role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Указанная роль не зарегистрирована")

    license_plate = payload.license_plate
    if role == ROLE_DRIVER and not license_plate:
        license_plate = payload.login

    new_user = User(
        login=payload.login,
        password_hash=hash_password(payload.password),
        role=role,
        vehicle_model=payload.vehicle_model,
        license_plate=license_plate,
        is_active=payload.is_active,
        can_track=payload.can_track,
        can_manage_users=payload.can_manage_users,
        can_view_logs=payload.can_view_logs,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    create_log(db, LogLevel.success, "admin.users", f"Создан пользователь {new_user.login}", user_id=new_user.id)
    return UserRead.model_validate(new_user)


@app.patch(
    "/admin/users/{user_id}/permissions",
    response_model=UserRead,
    tags=["Администрирование"],
    summary="Изменить права пользователя",
    description="Меняет роль, активность и дополнительные права доступа.",
)
def admin_update_user_permissions(
    user_id: int,
    payload: UserPermissionsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if payload.role is not None:
        next_role = payload.role.strip().lower()
        if not get_role_by_code(db, next_role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Указанная роль не зарегистрирована")
        user.role = next_role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.can_track is not None:
        user.can_track = payload.can_track
    if payload.can_manage_users is not None:
        user.can_manage_users = payload.can_manage_users
    if payload.can_view_logs is not None:
        user.can_view_logs = payload.can_view_logs
    if payload.vehicle_model is not None:
        user.vehicle_model = payload.vehicle_model
    if payload.license_plate is not None:
        user.license_plate = payload.license_plate

    db.commit()
    db.refresh(user)

    create_log(db, LogLevel.info, "admin.permissions", f"Изменены права пользователя {user.login}", user_id=user.id)
    return UserRead.model_validate(user)


@app.delete(
    "/admin/users/{user_id}",
    response_model=MessageResponse,
    tags=["Администрирование"],
    summary="Удалить пользователя",
    description="Удаляет пользователя из системы.",
)
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    login = user.login
    db.delete(user)
    db.commit()

    create_log(db, LogLevel.warning, "admin.users", f"Удален пользователь {login}")
    return MessageResponse(message="Пользователь удален")


@app.get(
    "/admin/roles",
    response_model=list[RoleRead],
    tags=["Администрирование"],
    summary="Получить список ролей",
    description="Возвращает зарегистрированные роли пользователей.",
)
def admin_list_roles(
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    rows = db.scalars(select(UserRoleEntry).order_by(UserRoleEntry.code)).all()
    return [RoleRead.model_validate(row) for row in rows]


@app.post(
    "/admin/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Администрирование"],
    summary="Добавить новую роль",
    description="Создает новую роль, которую можно использовать при создании пользователей.",
)
def admin_create_role(
    payload: RoleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    code = payload.code.strip().lower()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Код роли не может быть пустым")
    if get_role_by_code(db, code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Роль уже существует")

    role = UserRoleEntry(code=code, description=payload.description, is_system=False)
    db.add(role)
    db.commit()
    db.refresh(role)
    create_log(db, LogLevel.success, "admin.roles", f"Добавлена роль {code}", user_id=current_user.id)
    return RoleRead.model_validate(role)


# =========================
# LOGS
# =========================
@app.get(
    "/admin/logs",
    response_model=list[LogRead],
    tags=["Логи"],
    summary="Получить системные логи",
    description="Возвращает журнал событий системы с возможностью фильтрации.",
)
def admin_get_logs(
    level: Optional[LogLevel] = None,
    source: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require_log_access),
):
    query = select(SystemLog)

    if level is not None:
        query = query.where(SystemLog.level == level)
    if source is not None:
        query = query.where(SystemLog.source == source)

    query = query.order_by(desc(SystemLog.created_at)).limit(min(max(limit, 1), 500))
    rows = db.scalars(query).all()

    return [LogRead.model_validate(row) for row in rows]


@app.get(
    "/admin/logs/errors",
    response_model=list[LogRead],
    tags=["Логи"],
    summary="Получить ошибки и предупреждения",
    description="Возвращает только предупреждения и ошибки из системного журнала.",
)
def admin_get_error_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require_log_access),
):
    rows = db.scalars(
        select(SystemLog)
        .where(SystemLog.level.in_([LogLevel.warning, LogLevel.error]))
        .order_by(desc(SystemLog.created_at))
        .limit(min(max(limit, 1), 500))
    ).all()

    return [LogRead.model_validate(row) for row in rows]


@app.post(
    "/admin/logs",
    response_model=LogRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Логи"],
    summary="Создать лог-запись",
    description="Создает новую запись в системном журнале.",
)
def admin_create_log(
    payload: LogCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_log_access),
):
    log = create_log(
        db=db,
        level=payload.level,
        source=payload.source,
        message=payload.message,
        user_id=payload.user_id,
        extra_json=payload.extra_json,
    )
    return LogRead.model_validate(log)


# =========================
# DRIVER
# =========================
@app.post(
    "/route/start",
    response_model=RouteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Водитель"],
    summary="Начать рейс",
    description="Создает новый активный рейс для водителя.",
)
def start_route(
    payload: RouteStartRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_DRIVER)),
):
    if not current_user.can_track:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="У водителя отключен доступ к геотрекингу",
        )

    existing_active_route = db.scalar(
        select(ActiveRoute).where(
            ActiveRoute.driver_id == current_user.id,
            ActiveRoute.status == RouteStatus.active,
        )
    )
    if existing_active_route:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="У водителя уже есть активный рейс",
        )

    route = ActiveRoute(
        driver_id=current_user.id,
        start_name=payload.start_name,
        start_lat=payload.start_lat,
        start_lng=payload.start_lng,
        end_name=payload.end_name,
        end_lat=payload.end_lat,
        end_lng=payload.end_lng,
        start_time=payload.start_time,
        status=RouteStatus.active,
    )
    db.add(route)
    db.commit()
    db.refresh(route)

    create_log(
        db,
        LogLevel.success,
        "route.start",
        f"Водитель {current_user.login} начал рейс {payload.start_name} -> {payload.end_name}",
        user_id=current_user.id,
    )

    return RouteRead.model_validate(route)


@app.post(
    "/route/finish",
    response_model=MessageResponse,
    tags=["Водитель"],
    summary="Завершить рейс",
    description="Помечает текущий активный рейс водителя как завершенный.",
)
def finish_route(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_DRIVER)),
):
    route = db.scalar(
        select(ActiveRoute).where(
            ActiveRoute.driver_id == current_user.id,
            ActiveRoute.status == RouteStatus.active,
        )
    )
    if not route:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Активный рейс не найден",
        )

    route.status = RouteStatus.finished
    db.commit()

    create_log(
        db,
        LogLevel.info,
        "route.finish",
        f"Водитель {current_user.login} завершил рейс #{route.id}",
        user_id=current_user.id,
    )

    return MessageResponse(message="Рейс завершен")


@app.post(
    "/location/update",
    response_model=LocationRead,
    tags=["Водитель"],
    summary="Обновить координаты",
    description="Сохраняет текущую геопозицию водителя.",
)
def update_location(
    payload: LocationUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_DRIVER)),
):
    if not current_user.can_track:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="У водителя отключен доступ к геотрекингу",
        )

    active_route = db.scalar(
        select(ActiveRoute).where(
            ActiveRoute.driver_id == current_user.id,
            ActiveRoute.status == RouteStatus.active,
        )
    )
    if not active_route:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя обновлять локацию без активного рейса",
        )

    location = db.get(Location, current_user.id)
    now = datetime.now(timezone.utc)

    if location is None:
        location = Location(
            user_id=current_user.id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            updated_at=now,
        )
        db.add(location)
    else:
        delta_seconds = (now - location.updated_at).total_seconds()
        if delta_seconds > 9:
            create_log(
                db,
                LogLevel.warning,
                "location.update",
                f"Задержка обновления координат водителя {current_user.login}: {int(delta_seconds)} сек.",
                user_id=current_user.id,
            )

        location.latitude = payload.latitude
        location.longitude = payload.longitude
        location.updated_at = now

    db.commit()
    db.refresh(location)

    return LocationRead(
        user_id=location.user_id,
        latitude=location.latitude,
        longitude=location.longitude,
        updated_at=location.updated_at,
    )


# =========================
# PASSENGER / GLOBAL
# =========================
@app.get(
    "/routes/active",
    response_model=list[ActiveRouteRead],
    tags=["Пассажир"],
    summary="Получить активные рейсы",
    description="Возвращает список автобусов, которые сейчас находятся в пути.",
)
def get_active_routes(
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN, ROLE_DRIVER, ROLE_PASSENGER, ROLE_CUSTOMER)),
):
    routes = db.scalars(
        select(ActiveRoute)
        .where(ActiveRoute.status == RouteStatus.active)
        .order_by(ActiveRoute.id.desc())
    ).all()

    result: list[ActiveRouteRead] = []
    for route in routes:
        driver = route.driver
        result.append(
            ActiveRouteRead(
                id=route.id,
                driver_id=route.driver_id,
                driver_login=driver.login,
                vehicle_model=driver.vehicle_model,
                license_plate=driver.license_plate,
                start_name=route.start_name,
                end_name=route.end_name,
                start_time=route.start_time,
                status=route.status,
            )
        )
    return result


@app.get(
    "/location/{user_id}",
    response_model=LocationRead,
    tags=["Пассажир"],
    summary="Получить координаты водителя",
    description="Возвращает последнюю известную геопозицию выбранного водителя.",
)
def get_driver_location(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN, ROLE_DRIVER, ROLE_PASSENGER, ROLE_CUSTOMER)),
):
    user = get_user_by_id(db, user_id)
    if not user or user.role != ROLE_DRIVER:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Водитель не найден")

    active_route = db.scalar(
        select(ActiveRoute).where(
            ActiveRoute.driver_id == user_id,
            ActiveRoute.status == RouteStatus.active,
        )
    )
    if not active_route:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="У водителя нет активного рейса",
        )

    location = db.get(Location, user_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Локация водителя еще не получена",
        )

    return LocationRead(
        user_id=location.user_id,
        latitude=location.latitude,
        longitude=location.longitude,
        updated_at=location.updated_at,
    )


def _to_bus_request_read(item: BusRequest) -> BusRequestRead:
    return BusRequestRead(
        id=item.id,
        requester_id=item.requester_id,
        requester_login=item.requester.login,
        requester_kind=item.requester_kind,
        company_name=item.company_name,
        route_from=item.route_from,
        route_to=item.route_to,
        trip_time=item.trip_time,
        passenger_count=item.passenger_count,
        comment=item.comment,
        status=item.status,
        rejection_reason=item.rejection_reason,
        processed_by=item.processed_by,
        processed_by_login=item.processor.login if item.processor else None,
        created_at=item.created_at,
        processed_at=item.processed_at,
    )


@app.post(
    "/requests",
    response_model=BusRequestRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Пассажир"],
    summary="Создать заявку на автобус",
    description="Заказчик (частное лицо или компания) создает заявку, которую администратор может принять или отклонить.",
)
def create_bus_request(
    payload: BusRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_CUSTOMER, ROLE_ADMIN)),
):
    if payload.requester_kind == CustomerKind.company and not (payload.company_name or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Для компании укажите название")

    item = BusRequest(
        requester_id=current_user.id,
        requester_kind=payload.requester_kind,
        company_name=(payload.company_name or "").strip() or None,
        route_from=payload.route_from,
        route_to=payload.route_to,
        trip_time=payload.trip_time,
        passenger_count=payload.passenger_count,
        comment=payload.comment,
        status=RequestStatus.pending,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    create_log(db, LogLevel.info, "requests", f"Новая заявка #{item.id}", user_id=current_user.id)
    return _to_bus_request_read(item)


@app.get(
    "/admin/requests",
    response_model=list[BusRequestRead],
    tags=["Администрирование"],
    summary="Список заявок",
    description="Получить список заявок. По умолчанию — только pending.",
)
def admin_list_bus_requests(
    status_filter: Optional[RequestStatus] = RequestStatus.pending,
    db: Session = Depends(get_db),
    _: User = Depends(require_role(ROLE_ADMIN)),
):
    query = select(BusRequest).order_by(desc(BusRequest.created_at))
    if status_filter is not None:
        query = query.where(BusRequest.status == status_filter)
    rows = db.scalars(query).all()
    return [_to_bus_request_read(item) for item in rows]


@app.post(
    "/admin/requests/{request_id}/approve",
    response_model=BusRequestRead,
    tags=["Администрирование"],
    summary="Принять заявку",
)
def admin_approve_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    item = db.get(BusRequest, request_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка не найдена")
    item.status = RequestStatus.approved
    item.rejection_reason = None
    item.processed_by = current_user.id
    item.processed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    create_log(db, LogLevel.success, "requests", f"Заявка #{item.id} одобрена", user_id=current_user.id)
    return _to_bus_request_read(item)


@app.post(
    "/admin/requests/{request_id}/reject",
    response_model=BusRequestRead,
    tags=["Администрирование"],
    summary="Отклонить заявку",
)
def admin_reject_request(
    request_id: int,
    payload: BusRequestDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(ROLE_ADMIN)),
):
    item = db.get(BusRequest, request_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка не найдена")
    item.status = RequestStatus.rejected
    item.rejection_reason = (payload.rejection_reason or "").strip() or "Без комментария"
    item.processed_by = current_user.id
    item.processed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    create_log(db, LogLevel.warning, "requests", f"Заявка #{item.id} отклонена", user_id=current_user.id)
    return _to_bus_request_read(item)


# =========================
# SYSTEM
# =========================
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Система"],
    summary="Проверка состояния сервера",
    description="Возвращает простой ответ о работоспособности API.",
)
def healthcheck():
    return HealthResponse(status="ok", base_url=BASE_URL)


# =========================
# RUN
# =========================
# Запуск:
# uvicorn service_bus_backend_main:app --host 0.0.0.0 --port 8000
# gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 service_bus_backend_main:app
#
# Зависимости:
# pip install fastapi uvicorn gunicorn sqlalchemy psycopg2-binary python-jose[cryptography] passlib[bcrypt] bcrypt==4.0.1 python-multipart
#
# Важно:
# если у вас старая база и проблемы со входом, удалите service_bus.db и запустите сервер заново.
#
# Стандартный админ:
# login: admin
# password: admin123
