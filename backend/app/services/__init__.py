"""Application services.

Business logic that coordinates models, external clients, and Celery tasks
lives here, kept independent of the FastAPI request/response cycle so it
can be reused by API endpoints, workers, and tests alike.
"""
