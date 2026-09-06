"""兼容层 — 从 mock_commerce.routes 重新导出 app。"""
from mock_commerce.routes import app, main  # noqa: F401

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()