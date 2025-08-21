import abc
import dataclasses
import json
import logging
import os
import typing

import httpx

# Configure a module-level logger
logger = logging.getLogger(__name__)


# =============================================================================
# Custom Exceptions
# =============================================================================

class LLMClientError(Exception):
    """Base exception for all errors raised by LLM clients."""
    pass


class LLMConfigurationError(LLMClientError):
    """Raised when there's a configuration issue with the LLM client."""
    pass


class LLMAPIError(LLMClientError):
    """Raised when the LLM API returns an error response.

    Attributes:
        status_code (int): The HTTP status code of the error response.
        error_details (dict):
The JSON content of the error response from the API.
    """
    def __init__(self, message: str, status_code: int, error_details: dict):
        super().__init__(f"{message} (Status: {status_code}) - Details: {error_details}")
        self.status_code = status_code
        self.error_details = error_details


class LLMResponseParsingError(LLMClientError):
    """Raised when the LLM API response cannot be parsed as expected."""
    pass

# =============================================================================
# Data Structures
# =============================================================================

@dataclasses.dataclass(frozen=True)
class LLMMessage:
    """Represents a single message in a conversation.

    Attributes:
        role (str): The role of the message author (e.g., 'system', 'user',
            'assistant').
        content (str): The text content of the message.
    """
    role: str
    content: str


@dataclasses.dataclass(frozen=True)
class LLMRequest:
    """Encapsulates all parameters for a request to an LLM.

    Attributes:
        messages (typing.List[LLMMessage]): A list of messages forming the
            conversation history and the current prompt.
        model (str): The identifier of the model to use.
        temperature (float): Controls randomness. Lower is more deterministic.
        max_tokens (int): The maximum number of tokens to generate.
        stream (bool): Whether to stream the response or not. Note: The current
            client implementations are synchronous and do not support streaming.
            This is for future compatibility.
    """
    messages: typing.List[LLMMessage]
    model: str
    temperature: float = 0.7
    max_tokens: int = 1024
    stream: bool = False


@dataclasses.dataclass(frozen=True)
class LLMUsage:
    """Represents the token usage for a completed LLM request.

    Attributes:
        prompt_tokens (int): Number of tokens in the input prompt.
        completion_tokens (int): Number of tokens in the generated completion.
        total_tokens (int): Total tokens used in the request.
    """
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclasses.dataclass(frozen=True)
class LLMResponse:
    """A structured response object from an LLM API call.

    Attributes:
        content (str): The main text content of the LLM's response.
        model (str): The model that generated the response.
        usage (LLMUsage): Token usage statistics for the request.
        finish_reason (str): The reason the model stopped generating tokens.
        raw_response (dict): The original, unprocessed response dictionary from
            the API.
    """
    content: str
    model: str
    usage: LLMUsage
    finish_reason: str
    raw_response: typing.Dict[str, typing.Any]


# =============================================================================
# Abstract Base Class for LLM Clients
# =============================================================================

class LLMClient(abc.ABC):
    """Abstract base class for LLM API clients.

    This class defines the interface for interacting with various Large Language
    Model APIs, ensuring that different backends can be used interchangeably.
    """

    @abc.abstractmethod
    def generate_response(self, request: LLMRequest) -> LLMResponse:
        """Generates a response from the LLM based on the provided request.

        Args:
            request (LLMRequest): An object containing the prompt, model, and
                other parameters for the generation.

        Returns:
            LLMResponse: A structured object containing the LLM's response.

        Raises:
            LLMConfigurationError: If the client is not configured correctly.
            LLMAPIError: If the API returns an error.
            LLMResponseParsingError: If the API response is malformed.
        """
        raise NotImplementedError


# =============================================================================
# Concrete Implementations
# =============================================================================

class OpenAIClient(LLMClient):
    """An LLM client for interacting with OpenAI-compatible APIs.

    This client handles the construction of requests, making HTTP calls, and
    parsing responses from APIs that follow the OpenAI specification.
    """
    DEFAULT_API_BASE = "https://api.openai.com/v1"
    DEFAULT_TIMEOUT = 60.0  # in seconds

    def __init__(
        self, 
        api_key: typing.Optional[str] = None, 
        api_base: typing.Optional[str] = None, 
        timeout: float = DEFAULT_TIMEOUT
    ):
        """Initializes the OpenAIClient.

        Args:
            api_key (typing.Optional[str]): The API key. If not provided, it
                will be read from the 'OPENAI_API_KEY' environment variable.
            api_base (typing.Optional[str]): The base URL of the API. Defaults to
                the official OpenAI API URL.
            timeout (float): The timeout in seconds for API requests.

        Raises:
            LLMConfigurationError: If the API key is not provided and cannot be
                found in the environment variables.
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMConfigurationError(
                "OpenAI API key not provided. Please set the OPENAI_API_KEY "
                "environment variable or pass the api_key parameter."
            )
        self.api_base = api_base or self.DEFAULT_API_BASE
        self.timeout = timeout
        self.http_client = httpx.Client(timeout=self.timeout)

    def _prepare_headers(self) -> typing.Dict[str, str]:
        """Constructs the required HTTP headers for the API request."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _prepare_payload(self, request: LLMRequest) -> typing.Dict[str, typing.Any]:
        """Constructs the JSON payload for the API request."""
        return {
            "model": request.model,
            "messages": [
                dataclasses.asdict(msg) for msg in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": request.stream,
        }

    def _parse_response(self, response_data: typing.Dict[str, typing.Any]) -> LLMResponse:
        """Parses the raw JSON dictionary from the API into an LLMResponse object."""
        try:
            choice = response_data["choices"][0]
            message = choice["message"]
            usage_data = response_data["usage"]

            usage = LLMUsage(
                prompt_tokens=usage_data["prompt_tokens"],
                completion_tokens=usage_data["completion_tokens"],
                total_tokens=usage_data["total_tokens"],
            )

            return LLMResponse(
                content=message["content"],
                model=response_data["model"],
                usage=usage,
                finish_reason=choice["finish_reason"],
                raw_response=response_data,
            )
        except (KeyError, IndexError, TypeError) as e:
            logger.error("Failed to parse OpenAI response: %s", response_data)
            raise LLMResponseParsingError(f"Error parsing API response: {e}") from e

    def generate_response(self, request: LLMRequest) -> LLMResponse:
        """Generates a response from an OpenAI-compatible API."""
        if request.stream:
            logger.warning("Streaming is not supported by this synchronous client.")

        url = f"{self.api_base}/chat/completions"
        headers = self._prepare_headers()
        payload = self._prepare_payload(request)

        try:
            response = self.http_client.post(url, headers=headers, json=payload)
            response.raise_for_status()  # Raise HTTPError for 4xx/5xx responses
        except httpx.HTTPStatusError as e:
            error_details = e.response.json() if e.response.content else {}
            raise LLMAPIError(
                message="API returned an error",
                status_code=e.response.status_code,
                error_details=error_details,
            ) from e
        except httpx.RequestError as e:
            raise LLMAPIError(
                message=f"Request to API failed: {e}",
                status_code=500,  # Generic server-side error code
                error_details={}
            ) from e

        response_data = response.json()
        return self._parse_response(response_data)


class MockLLMClient(LLMClient):
    """A mock LLM client for testing and development.

    This client does not make any network requests. Instead, it returns a
    pre-configured, static response. It is useful for unit testing components
    that depend on an LLM without incurring API costs or network latency.
    """
    def __init__(self, mock_response_content: str = "This is a mock response."):
        """Initializes the MockLLMClient.

        Args:
            mock_response_content (str): The static content to be returned by
                the generate_response method.
        """
        self.mock_response_content = mock_response_content
        logger.info("Initialized MockLLMClient.")

    def generate_response(self, request: LLMRequest) -> LLMResponse:
        """Generates a mock response without making any API calls."""
        logger.info("Generating mock response for model: %s", request.model)

        usage = LLMUsage(
            prompt_tokens=sum(len(msg.content.split()) for msg in request.messages),
            completion_tokens=len(self.mock_response_content.split()),
            total_tokens=sum(len(msg.content.split()) for msg in request.messages) + len(self.mock_response_content.split()),
        )

        raw_response = {
            "id": "chatcmpl-mock-id",
            "object": "chat.completion",
            "created": 1677652288,
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": self.mock_response_content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": dataclasses.asdict(usage)
        }

        return LLMResponse(
            content=self.mock_response_content,
            model=request.model,
            usage=usage,
            finish_reason="stop",
            raw_response=raw_response,
        )


# =============================================================================
# Factory Function
# =============================================================================

_CLIENT_REGISTRY: typing.Dict[str, typing.Type[LLMClient]] = {
    "openai": OpenAIClient,
    "mock": MockLLMClient,
}


def get_llm_client(provider: str, **kwargs: typing.Any) -> LLMClient:
    """Factory function to get an instance of an LLM client.

    This function simplifies the creation of LLM clients by allowing you to
    specify the provider as a string.

    Args:
        provider (str): The name of the LLM provider (e.g., 'openai', 'mock').
        **kwargs: Keyword arguments to be passed to the client's constructor.

    Returns:
        LLMClient: An instance of the requested LLM client.

    Raises:
        ValueError: If the specified provider is not supported.
    """
    provider = provider.lower()
    client_class = _CLIENT_REGISTRY.get(provider)

    if client_class is None:
        available = ", ".join(_CLIENT_REGISTRY.keys())
        raise ValueError(
            f"Unsupported LLM provider: '{provider}'. "
            f"Available providers: {available}."
        )

    logger.info("Creating LLM client for provider: '%s'", provider)
    return client_class(**kwargs)
