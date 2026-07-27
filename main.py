from __future__ import annotations

import uvicorn

from src.state import state


if __name__ == "__main__":
    web_config = state.config.get("web", {})
    uvicorn.run(
        "src.api:app",
        host=web_config.get("host", "0.0.0.0"),
        port=int(web_config.get("port", 8000)),
        reload=False,
    )
