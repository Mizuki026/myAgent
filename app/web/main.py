"""Uvicorn entry point for the local web application."""

from .application import create_app

app = create_app()
