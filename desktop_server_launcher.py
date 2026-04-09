from __future__ import annotations

import json
import re
import socket
import sys
from dataclasses import dataclass
from typing import Any, Optional

import requests
from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)


# ======================================================
# CONFIG
# ======================================================
BACKEND_FILE = "service_bus_backend_main.py"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
LOCAL_API_BASE = f"http://{SERVER_HOST}:{SERVER_PORT}"
DEFAULT_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
# TUNNEL_EXE = "cloudflared.exe"  # использование cloudflared отключено
DEFAULT_DB_HOST = "127.0.0.1"
DEFAULT_DB_PORT = "5432"
DEFAULT_DB_NAME = "service_bus"
DEFAULT_DB_USER = "postgres"
DEFAULT_DB_PASSWORD = "postgres"
DEFAULT_JWT_SECRET = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
DEFAULT_JWT_EXPIRE_MIN = "1440"
DEFAULT_SQLITE_PATH = "./service_bus.db"


# ======================================================
# HTTP CLIENT
# ======================================================
@dataclass
class ApiSession:
    base_url: str = LOCAL_API_BASE
    token: Optional[str] = None

    def headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def login(self, username: str, password: str) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/auth/login",
            data={"username": username, "password": password},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        self.token = data["access_token"]
        return data

    def get_users(self) -> list[dict[str, Any]]:
        response = requests.get(f"{self.base_url}/admin/users", headers=self.headers(), timeout=10)
        response.raise_for_status()
        return response.json()

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/admin/users",
            headers={**self.headers(), "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def delete_user(self, user_id: int) -> dict[str, Any]:
        response = requests.delete(f"{self.base_url}/admin/users/{user_id}", headers=self.headers(), timeout=10)
        response.raise_for_status()
        return response.json()

    def update_permissions(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.patch(
            f"{self.base_url}/admin/users/{user_id}/permissions",
            headers={**self.headers(), "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_roles(self) -> list[dict[str, Any]]:
        response = requests.get(f"{self.base_url}/admin/roles", headers=self.headers(), timeout=10)
        response.raise_for_status()
        return response.json()

    def create_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/admin/roles",
            headers={**self.headers(), "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_logs(self) -> list[dict[str, Any]]:
        response = requests.get(f"{self.base_url}/admin/logs", headers=self.headers(), timeout=10)
        response.raise_for_status()
        return response.json()

    def get_error_logs(self) -> list[dict[str, Any]]:
        response = requests.get(f"{self.base_url}/admin/logs/errors", headers=self.headers(), timeout=10)
        response.raise_for_status()
        return response.json()

    def health(self) -> bool:
        response = requests.get(f"{self.base_url}/health", timeout=4)
        return response.ok


# ======================================================
# MAIN WINDOW
# ======================================================
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Служебный Автобус — Сервер и Админ-панель")
        self.resize(1400, 900)

        self.api = ApiSession()
        self.server_process = QProcess(self)
        self.current_user_login: Optional[str] = None
        self.tunnel_process = QProcess(self)
        self.public_url: Optional[str] = None
        self.server_bind_host = "0.0.0.0"

        self.server_process.readyReadStandardOutput.connect(self._read_server_stdout)
        self.server_process.readyReadStandardError.connect(self._read_server_stderr)
        self.tunnel_process.readyReadStandardOutput.connect(self._read_tunnel_stdout)
        self.tunnel_process.readyReadStandardError.connect(self._read_tunnel_stderr)
        self.server_process.finished.connect(lambda *_: self._append_system_log("[СЕРВЕР] Процесс остановлен"))
        self.tunnel_process.finished.connect(lambda *_: self._append_system_log("[СЕТЬ] Публичный туннель остановлен"))

        self.health_timer = QTimer(self)
        self.health_timer.setInterval(3000)
        self.health_timer.timeout.connect(self.refresh_health_status)
        self.health_timer.start()

        self._build_ui()
        self._build_toolbar()
        self.statusBar().showMessage("Готово к запуску")

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_top_panel())
        splitter.addWidget(self._build_bottom_tabs())
        splitter.setSizes([280, 620])

        root_layout.addWidget(splitter)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Основные действия")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        action_start_server = QAction("Запустить сервер", self)
        action_start_server.triggered.connect(self.start_server)
        toolbar.addAction(action_start_server)

        action_stop_server = QAction("Остановить сервер", self)
        action_stop_server.triggered.connect(self.stop_server)
        toolbar.addAction(action_stop_server)

        action_open_tunnel = QAction("Открыть внешний доступ", self)
        action_open_tunnel.triggered.connect(self.start_tunnel)
        toolbar.addAction(action_open_tunnel)

        action_close_tunnel = QAction("Закрыть внешний доступ", self)
        action_close_tunnel.triggered.connect(self.stop_tunnel)
        toolbar.addAction(action_close_tunnel)

        action_refresh = QAction("Обновить данные", self)
        action_refresh.triggered.connect(self.refresh_all)
        toolbar.addAction(action_refresh)

    def _build_top_panel(self) -> QWidget:
        widget = QWidget()
        layout = QGridLayout(widget)

        server_box = QGroupBox("Сервер")
        server_layout = QFormLayout(server_box)
        self.server_status = QLabel("Остановлен")
        self.server_url = QLabel(LOCAL_API_BASE)
        server_layout.addRow("Статус:", self.server_status)
        server_layout.addRow("Локальный адрес:", self.server_url)
        server_layout.addRow("Bind host:", QLabel(self.server_bind_host))

        network_box = QGroupBox("Сеть")
        network_layout = QFormLayout(network_box)
        self.public_url_label = QLabel("Не опубликован")
        network_layout.addRow("Публичный адрес:", self.public_url_label)

        auth_box = QGroupBox("Авторизация админа")
        auth_layout = QFormLayout(auth_box)
        self.login_input = QLineEdit(DEFAULT_ADMIN_LOGIN)
        self.password_input = QLineEdit(DEFAULT_ADMIN_PASSWORD)
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("Войти")
        self.login_button.clicked.connect(self.login_admin)
        self.auth_user_label = QLabel("Не авторизован")
        auth_layout.addRow("Логин:", self.login_input)
        auth_layout.addRow("Пароль:", self.password_input)
        auth_layout.addRow(self.login_button)
        auth_layout.addRow("Текущий пользователь:", self.auth_user_label)

        actions_box = QGroupBox("Быстрые действия")
        actions_layout = QVBoxLayout(actions_box)
        self.start_server_button = QPushButton("Запустить сервер")
        self.start_server_button.clicked.connect(self.start_server)
        self.stop_server_button = QPushButton("Остановить сервер")
        self.stop_server_button.clicked.connect(self.stop_server)
        self.start_tunnel_button = QPushButton("Открыть внешний доступ")
        self.start_tunnel_button.clicked.connect(self.start_tunnel)
        self.stop_tunnel_button = QPushButton("Закрыть внешний доступ")
        self.stop_tunnel_button.clicked.connect(self.stop_tunnel)
        for button in [self.start_server_button, self.stop_server_button, self.start_tunnel_button, self.stop_tunnel_button]:
            actions_layout.addWidget(button)

        config_box = self._build_server_config_box()

        layout.addWidget(server_box, 0, 0)
        layout.addWidget(network_box, 0, 1)
        layout.addWidget(auth_box, 0, 2)
        layout.addWidget(actions_box, 0, 3)
        layout.addWidget(config_box, 1, 0, 1, 4)
        return widget

    def _build_server_config_box(self) -> QWidget:
        config_box = QGroupBox("Конфигурация сервера (PostgreSQL / SQLite)")
        config_layout = QGridLayout(config_box)

        self.db_mode_input = QComboBox()
        self.db_mode_input.addItems(["PostgreSQL", "SQLite"])
        self.db_mode_input.currentTextChanged.connect(self._update_db_mode_fields)
        self.sqlite_path_input = QLineEdit(DEFAULT_SQLITE_PATH)
        self.db_host_input = QLineEdit(DEFAULT_DB_HOST)
        self.db_port_input = QLineEdit(DEFAULT_DB_PORT)
        self.db_name_input = QLineEdit(DEFAULT_DB_NAME)
        self.db_user_input = QLineEdit(DEFAULT_DB_USER)
        self.db_password_input = QLineEdit(DEFAULT_DB_PASSWORD)
        self.db_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.jwt_secret_input = QLineEdit(DEFAULT_JWT_SECRET)
        self.jwt_expire_input = QLineEdit(DEFAULT_JWT_EXPIRE_MIN)

        config_layout.addWidget(QLabel("DB mode"), 0, 0)
        config_layout.addWidget(self.db_mode_input, 0, 1)
        config_layout.addWidget(QLabel("SQLite path"), 0, 2)
        config_layout.addWidget(self.sqlite_path_input, 0, 3)

        config_layout.addWidget(QLabel("DB host"), 1, 0)
        config_layout.addWidget(self.db_host_input, 1, 1)
        config_layout.addWidget(QLabel("DB port"), 1, 2)
        config_layout.addWidget(self.db_port_input, 1, 3)

        config_layout.addWidget(QLabel("DB name"), 2, 0)
        config_layout.addWidget(self.db_name_input, 2, 1)
        config_layout.addWidget(QLabel("DB user"), 2, 2)
        config_layout.addWidget(self.db_user_input, 2, 3)

        config_layout.addWidget(QLabel("DB password"), 3, 0)
        config_layout.addWidget(self.db_password_input, 3, 1)
        config_layout.addWidget(QLabel("JWT secret"), 3, 2)
        config_layout.addWidget(self.jwt_secret_input, 3, 3)

        config_layout.addWidget(QLabel("JWT expire (min)"), 4, 0)
        config_layout.addWidget(self.jwt_expire_input, 4, 1)
        self._update_db_mode_fields()

        return config_box

    def _build_bottom_tabs(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_dashboard_tab(), "Панель")
        tabs.addTab(self._build_users_tab(), "Пользователи")
        tabs.addTab(self._build_roles_tab(), "Роли")
        tabs.addTab(self._build_logs_tab(), "Логи")
        tabs.addTab(self._build_network_tab(), "Сеть")
        return tabs

    def _build_dashboard_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.system_console = QTextEdit()
        self.system_console.setReadOnly(True)
        self.system_console.setPlaceholderText("Здесь будут отображаться логи сервера, туннеля и действий GUI")
        layout.addWidget(self.system_console)
        return widget

    def _build_users_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form_box = QGroupBox("Создание пользователя")
        form_layout = QGridLayout(form_box)
        self.new_login = QLineEdit()
        self.new_password = QLineEdit()
        self.new_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_role = QComboBox()
        self.new_vehicle = QLineEdit()
        self.new_plate = QLineEdit()
        self.new_active = QCheckBox()
        self.new_active.setChecked(True)
        self.new_track = QCheckBox()
        self.new_track.setChecked(True)
        self.new_manage = QCheckBox()
        self.new_logs = QCheckBox()
        create_button = QPushButton("Создать пользователя")
        create_button.clicked.connect(self.create_user)

        form_layout.addWidget(QLabel("Логин"), 0, 0)
        form_layout.addWidget(self.new_login, 0, 1)
        form_layout.addWidget(QLabel("Пароль"), 0, 2)
        form_layout.addWidget(self.new_password, 0, 3)
        form_layout.addWidget(QLabel("Роль"), 1, 0)
        form_layout.addWidget(self.new_role, 1, 1)
        form_layout.addWidget(QLabel("Модель"), 1, 2)
        form_layout.addWidget(self.new_vehicle, 1, 3)
        form_layout.addWidget(QLabel("Госномер"), 2, 0)
        form_layout.addWidget(self.new_plate, 2, 1)
        form_layout.addWidget(QLabel("Активен"), 2, 2)
        form_layout.addWidget(self.new_active, 2, 3)
        form_layout.addWidget(QLabel("Трекинг"), 3, 0)
        form_layout.addWidget(self.new_track, 3, 1)
        form_layout.addWidget(QLabel("Упр. пользователями"), 3, 2)
        form_layout.addWidget(self.new_manage, 3, 3)
        form_layout.addWidget(QLabel("Просмотр логов"), 4, 0)
        form_layout.addWidget(self.new_logs, 4, 1)
        form_layout.addWidget(create_button, 4, 3)

        search_layout = QHBoxLayout()
        self.user_search_input = QLineEdit()
        self.user_search_input.setPlaceholderText("Поиск по логину, роли, модели, номеру")
        self.user_search_input.textChanged.connect(self.filter_users_table)
        search_layout.addWidget(QLabel("Поиск:"))
        search_layout.addWidget(self.user_search_input)

        self.users_table = QTableWidget(0, 9)
        self.users_table.setHorizontalHeaderLabels([
            "ID",
            "Логин",
            "Роль",
            "Модель",
            "Госномер",
            "Активен",
            "Трекинг",
            "Упр. пользователями",
            "Логи",
        ])
        self.users_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        buttons_layout = QHBoxLayout()
        refresh_button = QPushButton("Обновить список")
        refresh_button.clicked.connect(self.load_users)
        delete_button = QPushButton("Удалить выбранного")
        delete_button.clicked.connect(self.delete_selected_user)
        enable_logs_button = QPushButton("Выдать доступ к логам")
        enable_logs_button.clicked.connect(self.enable_logs_for_selected)
        buttons_layout.addWidget(refresh_button)
        buttons_layout.addWidget(delete_button)
        buttons_layout.addWidget(enable_logs_button)
        buttons_layout.addStretch(1)

        self.users_count_label = QLabel("Пользователей загружено: 0")

        layout.addWidget(form_box)
        layout.addLayout(search_layout)
        layout.addLayout(buttons_layout)
        layout.addWidget(self.users_count_label)
        layout.addWidget(self.users_table)
        return widget

    def _build_logs_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        buttons_layout = QHBoxLayout()
        load_logs_button = QPushButton("Все логи")
        load_logs_button.clicked.connect(self.load_logs)
        load_errors_button = QPushButton("Ошибки и предупреждения")
        load_errors_button.clicked.connect(self.load_error_logs)
        buttons_layout.addWidget(load_logs_button)
        buttons_layout.addWidget(load_errors_button)
        buttons_layout.addStretch(1)

        self.logs_table = QTableWidget(0, 5)
        self.logs_table.setHorizontalHeaderLabels(["ID", "Уровень", "Источник", "Сообщение", "Создан"])
        self.logs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        layout.addLayout(buttons_layout)
        layout.addWidget(self.logs_table)
        return widget

    def _build_roles_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form_layout = QGridLayout()
        self.new_role_code_input = QLineEdit()
        self.new_role_desc_input = QLineEdit()
        add_role_button = QPushButton("Добавить роль")
        add_role_button.clicked.connect(self.create_role)
        load_roles_button = QPushButton("Обновить роли")
        load_roles_button.clicked.connect(self.load_roles)

        form_layout.addWidget(QLabel("Код роли"), 0, 0)
        form_layout.addWidget(self.new_role_code_input, 0, 1)
        form_layout.addWidget(QLabel("Описание"), 0, 2)
        form_layout.addWidget(self.new_role_desc_input, 0, 3)
        form_layout.addWidget(add_role_button, 0, 4)
        form_layout.addWidget(load_roles_button, 0, 5)

        self.roles_table = QTableWidget(0, 3)
        self.roles_table.setHorizontalHeaderLabels(["Код", "Описание", "Системная"])
        self.roles_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        layout.addLayout(form_layout)
        layout.addWidget(self.roles_table)
        return widget

    def _build_network_tab(self) -> QWidget:
        widget = QWidget()
        layout = QFormLayout(widget)
        self.network_local = QLabel(LOCAL_API_BASE)
        self.network_public = QLabel("Не опубликован")
        self.network_help = QLabel("Android-приложение должно использовать публичный URL или локальный IP.")
        self.network_help.setWordWrap(True)
        layout.addRow("Локальный API:", self.network_local)
        layout.addRow("Публичный API:", self.network_public)
        layout.addRow("Примечание:", self.network_help)
        return widget

    # ---------------- Server/Tunnel ----------------
    def _build_database_url(self) -> str:
        if self.db_mode_input.currentText() == "SQLite":
            return f"sqlite:///{self.sqlite_path_input.text().strip()}"
        host = self.db_host_input.text().strip()
        port = self.db_port_input.text().strip()
        name = self.db_name_input.text().strip()
        user = self.db_user_input.text().strip()
        password = self.db_password_input.text()
        return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

    def _masked_database_url(self) -> str:
        if self.db_mode_input.currentText() == "SQLite":
            return f"sqlite:///{self.sqlite_path_input.text().strip()}"
        host = self.db_host_input.text().strip()
        port = self.db_port_input.text().strip()
        name = self.db_name_input.text().strip()
        user = self.db_user_input.text().strip()
        return f"postgresql+psycopg2://{user}:***@{host}:{port}/{name}"

    def _update_db_mode_fields(self) -> None:
        is_postgres = self.db_mode_input.currentText() == "PostgreSQL"
        for widget in [self.db_host_input, self.db_port_input, self.db_name_input, self.db_user_input, self.db_password_input]:
            widget.setEnabled(is_postgres)
        self.sqlite_path_input.setEnabled(not is_postgres)

    def _is_tcp_open(self, host: str, port: int, timeout: float = 1.5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _validate_server_config(self) -> Optional[str]:
        mode = self.db_mode_input.currentText()
        required_fields = {
            "JWT secret": self.jwt_secret_input.text().strip(),
            "JWT expire": self.jwt_expire_input.text().strip(),
        }
        if mode == "PostgreSQL":
            required_fields.update({
                "DB host": self.db_host_input.text().strip(),
                "DB port": self.db_port_input.text().strip(),
                "DB name": self.db_name_input.text().strip(),
                "DB user": self.db_user_input.text().strip(),
                "DB password": self.db_password_input.text(),
            })
        else:
            required_fields["SQLite path"] = self.sqlite_path_input.text().strip()
        for label, value in required_fields.items():
            if not value:
                return f"Поле '{label}' обязательно"
        if mode == "PostgreSQL" and not self.db_port_input.text().strip().isdigit():
            return "DB port должен быть числом"
        if not self.jwt_expire_input.text().strip().isdigit():
            return "JWT expire (min) должен быть числом"
        if mode == "PostgreSQL":
            host = self.db_host_input.text().strip()
            port = int(self.db_port_input.text().strip())
            if not self._is_tcp_open(host, port):
                return f"Нет соединения с PostgreSQL {host}:{port}. Запустите PostgreSQL или выберите SQLite."
        return None

    def start_server(self) -> None:
        if self.server_process.state() != QProcess.ProcessState.NotRunning:
            self._append_system_log("[СЕРВЕР] Сервер уже запущен")
            return
        config_error = self._validate_server_config()
        if config_error:
            QMessageBox.warning(self, "Конфигурация", config_error)
            return

        database_url = self._build_database_url()
        self._append_system_log(f"[СЕРВЕР] Режим: {self.db_mode_input.currentText()}")
        self._append_system_log(f"[СЕРВЕР] DATABASE_URL: {self._masked_database_url()}")
        self._append_system_log("[СЕРВЕР] Запуск FastAPI...")
        process_env = QProcessEnvironment.systemEnvironment()
        process_env.insert("DATABASE_URL", database_url)
        process_env.insert("JWT_SECRET_KEY", self.jwt_secret_input.text().strip())
        process_env.insert("ACCESS_TOKEN_EXPIRE_MINUTES", self.jwt_expire_input.text().strip())
        self.server_process.setProcessEnvironment(process_env)
        self.server_process.start(
            sys.executable,
            [
                "-m",
                "uvicorn",
                "service_bus_backend_main:app",
                "--host",
                self.server_bind_host,
                "--port",
                str(SERVER_PORT),
            ],
        )
        started = self.server_process.waitForStarted(5000)
        if started:
            self.server_status.setText("Запущен")
            self.statusBar().showMessage("Сервер запущен")
        else:
            self._append_system_log("[СЕРВЕР] Не удалось запустить процесс")
            QMessageBox.critical(self, "Ошибка", "Не удалось запустить сервер")

    def stop_server(self) -> None:
        if self.server_process.state() == QProcess.ProcessState.NotRunning:
            self._append_system_log("[СЕРВЕР] Сервер уже остановлен")
            return
        self.server_process.terminate()
        if not self.server_process.waitForFinished(5000):
            self.server_process.kill()
        self.server_status.setText("Остановлен")
        self.statusBar().showMessage("Сервер остановлен")

    def start_tunnel(self) -> None:
        self._append_system_log("[СЕТЬ] Запуск cloudflared отключен. Используйте белый IP и открытый порт 8000.")
        QMessageBox.information(
            self,
            "Сеть",
            "Использование cloudflared отключено. Для внешнего доступа используйте белый IP и открытый порт 8000.",
        )

    def stop_tunnel(self) -> None:
        self._append_system_log("[СЕТЬ] cloudflared отключен, останавливать нечего.")
        self.public_url = None
        self.public_url_label.setText("Не опубликован")
        self.network_public.setText("Не опубликован")

    # ---------------- Auth/API ----------------
    def login_admin(self) -> None:
        try:
            data = self.api.login(self.login_input.text().strip(), self.password_input.text())
            self.current_user_login = data.get("login")
            self.auth_user_label.setText(self.current_user_login or "Не авторизован")
            self.login_input.hide()
            self.password_input.hide()
            self.login_button.hide()
            self._append_system_log(f"[AUTH] Успешный вход: {data.get('login')}")
            self.statusBar().showMessage("Авторизация успешна")
            self.refresh_all()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка входа", str(exc))
            self._append_system_log(f"[AUTH] Ошибка входа: {exc}")

    def load_users(self) -> None:
        try:
            rows = self.api.get_users()
            self.all_users_cache = rows
            self.users_table.setRowCount(len(rows))
            for row_index, user in enumerate(rows):
                values = [
                    user.get("id"),
                    user.get("login"),
                    user.get("role"),
                    user.get("vehicle_model") or "",
                    user.get("license_plate") or "",
                    "Да" if user.get("is_active") else "Нет",
                    "Да" if user.get("can_track") else "Нет",
                    "Да" if user.get("can_manage_users") else "Нет",
                    "Да" if user.get("can_view_logs") else "Нет",
                ]
                for col, value in enumerate(values):
                    self.users_table.setItem(row_index, col, QTableWidgetItem(str(value)))
            self.users_count_label.setText(f"Пользователей загружено: {len(rows)}")
            self.filter_users_table()
            self._append_system_log(f"[USERS] Загружено пользователей: {len(rows)}")
        except Exception as exc:
            self._append_system_log(f"[USERS] Ошибка загрузки пользователей: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить пользователей:\n{exc}")

    def load_roles(self) -> None:
        try:
            rows = self.api.get_roles()
            self.roles_table.setRowCount(len(rows))
            self.new_role.blockSignals(True)
            self.new_role.clear()
            for row_index, role in enumerate(rows):
                self.new_role.addItem(role.get("code", ""))
                values = [
                    role.get("code", ""),
                    role.get("description") or "",
                    "Да" if role.get("is_system") else "Нет",
                ]
                for col, value in enumerate(values):
                    self.roles_table.setItem(row_index, col, QTableWidgetItem(str(value)))
            self.new_role.blockSignals(False)
            self._append_system_log(f"[ROLES] Загружено ролей: {len(rows)}")
        except Exception as exc:
            self._append_system_log(f"[ROLES] Ошибка загрузки ролей: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить роли:\n{exc}")

    def create_role(self) -> None:
        code = self.new_role_code_input.text().strip().lower()
        if not code:
            QMessageBox.warning(self, "Роли", "Укажите код роли")
            return
        payload = {"code": code, "description": self.new_role_desc_input.text().strip() or None}
        try:
            self.api.create_role(payload)
            self._append_system_log(f"[ROLES] Добавлена роль: {code}")
            self.new_role_code_input.clear()
            self.new_role_desc_input.clear()
            self.load_roles()
        except Exception as exc:
            self._append_system_log(f"[ROLES] Ошибка создания роли: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось добавить роль:\n{exc}")

    def create_user(self) -> None:
        try:
            role_value = self.new_role.currentText().strip()
            if not role_value:
                QMessageBox.warning(self, "Пользователи", "Сначала загрузите и выберите роль")
                return
            payload = {
                "login": self.new_login.text().strip(),
                "password": self.new_password.text(),
                "role": role_value,
                "vehicle_model": self.new_vehicle.text().strip() or None,
                "license_plate": self.new_plate.text().strip() or None,
                "is_active": self.new_active.isChecked(),
                "can_track": self.new_track.isChecked(),
                "can_manage_users": self.new_manage.isChecked(),
                "can_view_logs": self.new_logs.isChecked(),
            }
            self.api.create_user(payload)
            self._append_system_log(f"[USERS] Создан пользователь: {payload['login']}")
            self.load_users()
        except Exception as exc:
            self._append_system_log(f"[USERS] Ошибка создания пользователя: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось создать пользователя:\n{exc}")

    def delete_selected_user(self) -> None:
        row = self.users_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Удаление", "Сначала выберите пользователя в таблице")
            return
        user_id_item = self.users_table.item(row, 0)
        login_item = self.users_table.item(row, 1)
        if not user_id_item:
            return
        user_id = int(user_id_item.text())
        login = login_item.text() if login_item else str(user_id)
        try:
            self.api.delete_user(user_id)
            self._append_system_log(f"[USERS] Удален пользователь: {login}")
            self.load_users()
        except Exception as exc:
            self._append_system_log(f"[USERS] Ошибка удаления пользователя: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось удалить пользователя:\n{exc}")

    def enable_logs_for_selected(self) -> None:
        row = self.users_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Права", "Сначала выберите пользователя в таблице")
            return
        user_id = int(self.users_table.item(row, 0).text())
        login = self.users_table.item(row, 1).text()
        try:
            self.api.update_permissions(user_id, {"can_view_logs": True})
            self._append_system_log(f"[USERS] Пользователю {login} выдан доступ к логам")
            self.load_users()
        except Exception as exc:
            self._append_system_log(f"[USERS] Ошибка изменения прав: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось изменить права:\n{exc}")

    def load_logs(self) -> None:
        try:
            rows = self.api.get_logs()
            self._fill_logs_table(rows)
            self._append_system_log(f"[LOGS] Загружено логов: {len(rows)}")
        except Exception as exc:
            self._append_system_log(f"[LOGS] Ошибка загрузки логов: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить логи:\n{exc}")

    def load_error_logs(self) -> None:
        try:
            rows = self.api.get_error_logs()
            self._fill_logs_table(rows)
            self._append_system_log(f"[LOGS] Загружено ошибок и предупреждений: {len(rows)}")
        except Exception as exc:
            self._append_system_log(f"[LOGS] Ошибка загрузки ошибок: {exc}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить ошибки:\n{exc}")

    def filter_users_table(self) -> None:
        query = self.user_search_input.text().strip().lower()
        visible_count = 0
        for row in range(self.users_table.rowCount()):
            row_text_parts = []
            for col in range(self.users_table.columnCount()):
                item = self.users_table.item(row, col)
                if item:
                    row_text_parts.append(item.text().lower())
            haystack = " ".join(row_text_parts)
            is_visible = query in haystack if query else True
            self.users_table.setRowHidden(row, not is_visible)
            if is_visible:
                visible_count += 1
        self.users_count_label.setText(f"Пользователей показано: {visible_count} / {self.users_table.rowCount()}")

    def _fill_logs_table(self, rows: list[dict[str, Any]]) -> None:
        self.logs_table.setRowCount(len(rows))
        for row_index, item in enumerate(rows):
            values = [
                item.get("id"),
                item.get("level"),
                item.get("source"),
                item.get("message"),
                item.get("created_at"),
            ]
            for col, value in enumerate(values):
                self.logs_table.setItem(row_index, col, QTableWidgetItem(str(value)))

    # ---------------- Refresh ----------------
    def refresh_all(self) -> None:
        self.refresh_health_status()
        if self.api.token:
            self.load_roles()
            self.load_users()
            self.load_logs()

    def refresh_health_status(self) -> None:
        try:
            ok = self.api.health()
            self.server_status.setText("Запущен" if ok else "Недоступен")
        except Exception:
            self.server_status.setText("Остановлен")

    # ---------------- Process logs ----------------
    def _read_server_stdout(self) -> None:
        text = bytes(self.server_process.readAllStandardOutput()).decode("utf-8", errors="ignore")
        self._append_system_log(text.strip())

    def _read_server_stderr(self) -> None:
        text = bytes(self.server_process.readAllStandardError()).decode("utf-8", errors="ignore")
        self._append_system_log(text.strip())

    def _read_tunnel_stdout(self) -> None:
        text = bytes(self.tunnel_process.readAllStandardOutput()).decode("utf-8", errors="ignore")
        self._append_system_log(text.strip())
        self._extract_public_url(text)

    def _read_tunnel_stderr(self) -> None:
        text = bytes(self.tunnel_process.readAllStandardError()).decode("utf-8", errors="ignore")
        self._append_system_log(text.strip())
        self._extract_public_url(text)

    def _extract_public_url(self, text: str) -> None:
        match = re.search(r"https://[a-zA-Z0-9\-\.]+trycloudflare\.com", text)
        if match:
            self.public_url = match.group(0)
            self.public_url_label.setText(self.public_url)
            self.network_public.setText(self.public_url)
            self._append_system_log(f"[СЕТЬ] Публичный адрес: {self.public_url}")

    def _append_system_log(self, text: str) -> None:
        if not text:
            return
        self.system_console.append(text)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.server_process.state() != QProcess.ProcessState.NotRunning:
            self.stop_server()
        if self.tunnel_process.state() != QProcess.ProcessState.NotRunning:
            self.stop_tunnel()
        super().closeEvent(event)


# ======================================================
# ENTRYPOINT
# ======================================================
def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
