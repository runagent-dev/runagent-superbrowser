/**
 * HTTP error classes from browserless.
 * Throw these to return appropriate HTTP status codes.
 */

export class BrowserError extends Error {
  constructor(
    message: string,
    public statusCode: number = 500,
  ) {
    super(message);
    this.name = this.constructor.name;
  }
}

export class BadRequest extends BrowserError {
  constructor(message: string) {
    super(message, 400);
  }
}

export class Unauthorized extends BrowserError {
  constructor(message: string = 'Unauthorized') {
    super(message, 401);
  }
}

export class NotFound extends BrowserError {
  constructor(message: string = 'Not Found') {
    super(message, 404);
  }
}

export class TooManyRequests extends BrowserError {
  constructor(message: string = 'Too many requests') {
    super(message, 429);
  }
}

export class Timeout extends BrowserError {
  constructor(message: string = 'Request timed out') {
    super(message, 408);
  }
}

export class ServerError extends BrowserError {
  constructor(message: string = 'Internal server error') {
    super(message, 500);
  }
}
