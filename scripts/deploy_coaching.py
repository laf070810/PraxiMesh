"""Optional, operator-selected HTTPS tutor configuration for the Web container."""

import ssl


def coaching_environment(config):
    """Validate without contacting the provider; errors never contain credentials."""
    from coaching.provider import ProviderConfig, ProviderError

    fields = {
        "endpoint",
        "model",
        "token",
        "description",
        "daily_limit",
        "concurrency",
        "max_tokens",
        "disable_thinking",
    }
    try:
        if (
            not isinstance(config, dict)
            or not fields <= config.keys()
            or set(config) - fields - {"ca_pem"}
        ):
            raise ValueError
        if (
            not isinstance(config["description"], str)
            or not config["description"].strip()
            or len(config["description"]) > 2000
            or "\0" in config["description"]
            or type(config["daily_limit"]) is not int
            or not 1 <= config["daily_limit"] <= 100
            or type(config["concurrency"]) is not int
            or not 1 <= config["concurrency"] <= 4
        ):
            raise ValueError
        provider = ProviderConfig(
            endpoint=config["endpoint"],
            model=config["model"],
            token=config["token"],
            ca_pem=config.get("ca_pem"),
            max_tokens=config["max_tokens"],
            disable_thinking=config["disable_thinking"],
        )
        provider.validate()  # No plaintext or loopback override in the deployment coordinator.
        if provider.ca_pem:
            ssl.create_default_context(cadata=provider.ca_pem)
        return {
            "COACHING_ENABLED": "1",
            "COACHING_ENDPOINT": provider.endpoint,
            "COACHING_MODEL": provider.model,
            "COACHING_TOKEN": provider.token,
            "COACHING_PROVIDER_DESCRIPTION": config["description"],
            "COACHING_CA_PEM": provider.ca_pem or "",
            "COACHING_DISABLE_THINKING": "1" if provider.disable_thinking else "0",
            "COACHING_DAILY_LIMIT": str(config["daily_limit"]),
            "COACHING_CONCURRENCY": str(config["concurrency"]),
            "COACHING_MAX_TOKENS": str(provider.max_tokens),
        }
    except (KeyError, TypeError, ValueError, ssl.SSLError, ProviderError):
        raise ValueError("Invalid optional HTTPS coaching configuration") from None
