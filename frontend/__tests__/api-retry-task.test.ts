/**
 * Tests for the retry task API function.
 *
 * Test scenarios:
 * 1. API call: Mock api.retryTask and verify it's called with correct task ID
 * 2. Success response: Verify correct task data is returned
 * 3. Error handling: Verify errors are properly thrown
 */

import { api } from '@/lib/api';

// Mock fetch globally
global.fetch = jest.fn();

describe('api.retryTask', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // Reset fetch mock
    (global.fetch as jest.Mock).mockReset();
  });

  it('should call POST /api/tasks/{id}/retry with correct task ID', async () => {
    const mockTask = {
      id: 123,
      owner_id: 1,
      uri: 'https://example.com/file.zip',
      status: 'active',
      gid: 'new_gid_456',
      name: 'file.zip',
      total_length: 1000000,
      completed_length: 0,
      download_speed: 0,
      upload_speed: 0,
      created_at: '2024-01-23T00:00:00Z',
      updated_at: '2024-01-23T00:00:00Z',
    };

    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      json: async () => mockTask,
    });

    const result = await api.retryTask(42);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/tasks/42/retry'),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
        }),
      })
    );
    expect(result).toEqual(mockTask);
  });

  it('should return new task data on success', async () => {
    const mockNewTask = {
      id: 456,
      owner_id: 1,
      uri: 'https://example.com/retry-file.zip',
      status: 'queued',
      gid: 'retry_gid_789',
      name: 'retry-file.zip',
      total_length: 0,
      completed_length: 0,
      download_speed: 0,
      upload_speed: 0,
      created_at: '2024-01-23T12:00:00Z',
      updated_at: '2024-01-23T12:00:00Z',
    };

    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      json: async () => mockNewTask,
    });

    const result = await api.retryTask(123);

    expect(result.id).toBe(456);
    expect(result.uri).toBe('https://example.com/retry-file.zip');
    expect(result.status).toBe('queued');
    expect(result.gid).toBe('retry_gid_789');
  });

  it('should throw error when task not found (404)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 404,
      text: async () => '{"detail":"任务不存在"}',
    });

    await expect(api.retryTask(99999)).rejects.toThrow();
  });

  it('should throw error for torrent task (400)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 400,
      text: async () => '{"detail":"种子任务无法重试，请重新上传种子文件"}',
    });

    await expect(api.retryTask(123)).rejects.toThrow();
  });

  it('should throw error on aria2 failure (500)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: async () => '{"detail":"创建 aria2 任务失败: connection refused"}',
    });

    await expect(api.retryTask(123)).rejects.toThrow();
  });

  it('should throw error when not authenticated (401)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 401,
      text: async () => '{"detail":"Not authenticated"}',
    });

    await expect(api.retryTask(123)).rejects.toThrow();
  });
});
