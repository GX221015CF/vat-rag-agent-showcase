from __future__ import annotations

from tax_rag.config import load_toml
from tax_rag.web.app import create_app


def main() -> None:
    runtime = load_toml("runtime.toml")
    host = runtime["web"]["host"]
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("P2A禁止绑定到局域网或公网地址")
    app = create_app()
    app.run(
        host=host,
        port=int(runtime["web"]["port"]),
        debug=bool(runtime["web"]["debug"]),
        use_reloader=False,
    )


if __name__ == "__main__":
    main()

