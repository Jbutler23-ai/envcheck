import os


def settings():
    return {
        "api_url": os.environ["API_URL"],
        "timeout": os.getenv("REQUEST_TIMEOUT", "10"),
    }
