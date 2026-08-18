import type { ServerGateway, ServerRequest } from './serverGateway';

export class PlaceholderServerGateway implements ServerGateway {
  private baseUrl: string;

  constructor(baseUrl = '') {
    this.baseUrl = baseUrl.trim();
  }

  getBaseUrl(): string {
    return this.baseUrl;
  }

  setBaseUrl(value: string): string {
    this.baseUrl = value.trim();
    return this.baseUrl;
  }

  async request<T>(_request: ServerRequest): Promise<T> {
    throw new Error('Server HTTP 尚未接入');
  }
}
