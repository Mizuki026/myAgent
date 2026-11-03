"""Run the local web app on the loopback interface."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.web.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        workers=1,
        reload=False,
    )
