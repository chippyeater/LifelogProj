from api_server import DB_PATH, app, init_db
from runtime_config import get_config_value


def main() -> None:
    init_db(DB_PATH)
    host = str(get_config_value("server.host", "0.0.0.0"))
    port = int(get_config_value("server.port", 8000))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
