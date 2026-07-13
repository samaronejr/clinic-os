from urllib.parse import quote, urlsplit, urlunsplit


def database_url_for_name(configured_url: str, database_name: str) -> str:
    parsed_url = urlsplit(configured_url)
    encoded_name = quote(database_name, safe="")
    return urlunsplit(parsed_url._replace(path=f"/{encoded_name}"))
