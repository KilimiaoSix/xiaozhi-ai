export type ServerRequestMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export interface ServerRequest {
  method: ServerRequestMethod;
  path: string;
  body?: unknown;
}

export interface ServerGateway {
  getBaseUrl(): string;
  setBaseUrl(value: string): string;
  request<T>(request: ServerRequest): Promise<T>;
}
