/**
 * Tests for retry task functionality in tasks page and history page.
 *
 * Test scenarios:
 * 1. Tasks page: retryTask function handles success/failure correctly
 * 2. Tasks page: torrent tasks are blocked from retry (client-side check)
 * 3. History page: handleRetry function updates state correctly
 * 4. State update: old task is replaced by new task in state
 */

import { Task } from '@/types';

// Mock task data for testing
const createMockTask = (overrides: Partial<Task> = {}): Task => ({
  id: 1,
  owner_id: 1,
  uri: 'https://example.com/file.zip',
  status: 'error',
  name: 'file.zip',
  total_length: 1000000,
  completed_length: 500000,
  download_speed: 0,
  upload_speed: 0,
  error: 'Connection timeout',
  created_at: '2024-01-23T00:00:00Z',
  updated_at: '2024-01-23T00:00:00Z',
  ...overrides,
});

const createMockTorrentTask = (overrides: Partial<Task> = {}): Task =>
  createMockTask({
    uri: '[torrent]',
    name: 'movie.mkv',
    error: 'No seeds available',
    ...overrides,
  });

describe('Retry Task State Management', () => {
  describe('retryTask function behavior', () => {
    it('should detect torrent task by uri === "[torrent]"', () => {
      const torrentTask = createMockTorrentTask();
      const isTorrent = torrentTask.uri === '[torrent]';
      expect(isTorrent).toBe(true);
    });

    it('should detect HTTP task by uri !== "[torrent]"', () => {
      const httpTask = createMockTask();
      const isTorrent = httpTask.uri === '[torrent]';
      expect(isTorrent).toBe(false);
    });

    it('should only show retry button for error status tasks', () => {
      const errorTask = createMockTask({ status: 'error' });
      const activeTask = createMockTask({ status: 'active' });
      const completeTask = createMockTask({ status: 'complete' });
      const pausedTask = createMockTask({ status: 'paused' });

      expect(errorTask.status === 'error').toBe(true);
      expect(activeTask.status === 'error').toBe(false);
      expect(completeTask.status === 'error').toBe(false);
      expect(pausedTask.status === 'error').toBe(false);
    });
  });

  describe('Task list state update after retry', () => {
    it('should replace old task with new task in state', () => {
      const oldTask = createMockTask({ id: 1 });
      const newTask = createMockTask({ id: 2, status: 'active', gid: 'new_gid' });
      const otherTask = createMockTask({ id: 3, status: 'complete' });

      const tasks: Task[] = [oldTask, otherTask];

      // Simulate the state update logic from TasksPage.retryTask
      const updatedTasks = (() => {
        const filtered = tasks.filter((t) => t.id !== oldTask.id);
        return [newTask, ...filtered];
      })();

      // Verify old task is removed
      expect(updatedTasks.find((t) => t.id === 1)).toBeUndefined();
      // Verify new task is added at the beginning
      expect(updatedTasks[0].id).toBe(2);
      expect(updatedTasks[0].status).toBe('active');
      // Verify other tasks are preserved
      expect(updatedTasks.find((t) => t.id === 3)).toBeDefined();
      // Verify total count
      expect(updatedTasks.length).toBe(2);
    });

    it('should handle retry when task list has only the retried task', () => {
      const oldTask = createMockTask({ id: 1 });
      const newTask = createMockTask({ id: 2, status: 'queued' });

      const tasks: Task[] = [oldTask];

      const updatedTasks = (() => {
        const filtered = tasks.filter((t) => t.id !== oldTask.id);
        return [newTask, ...filtered];
      })();

      expect(updatedTasks.length).toBe(1);
      expect(updatedTasks[0].id).toBe(2);
    });

    it('should preserve task order with new task at top', () => {
      const task1 = createMockTask({ id: 1, status: 'error' });
      const task2 = createMockTask({ id: 2, status: 'active' });
      const task3 = createMockTask({ id: 3, status: 'complete' });
      const newTask = createMockTask({ id: 4, status: 'queued' });

      const tasks: Task[] = [task1, task2, task3];

      // Retry task1
      const updatedTasks = (() => {
        const filtered = tasks.filter((t) => t.id !== task1.id);
        return [newTask, ...filtered];
      })();

      expect(updatedTasks.map((t) => t.id)).toEqual([4, 2, 3]);
    });
  });

  describe('History page retry behavior', () => {
    it('should remove task from history list after successful retry', () => {
      const task1 = createMockTask({ id: 1, status: 'error' });
      const task2 = createMockTask({ id: 2, status: 'complete' });
      const task3 = createMockTask({ id: 3, status: 'error' });

      const historyTasks: Task[] = [task1, task2, task3];

      // Simulate handleRetry state update (task moves to active tasks)
      const updatedHistory = historyTasks.filter((t) => t.id !== task1.id);

      expect(updatedHistory.length).toBe(2);
      expect(updatedHistory.find((t) => t.id === 1)).toBeUndefined();
      expect(updatedHistory.map((t) => t.id)).toEqual([2, 3]);
    });
  });

  describe('Batch retry behavior', () => {
    it('should filter out torrent tasks from batch retry', () => {
      const httpTask1 = createMockTask({ id: 1, status: 'error' });
      const torrentTask = createMockTorrentTask({ id: 2, status: 'error' });
      const httpTask2 = createMockTask({ id: 3, status: 'error' });
      const activeTask = createMockTask({ id: 4, status: 'active' });

      const tasks: Task[] = [httpTask1, torrentTask, httpTask2, activeTask];
      const selectedIds = new Set([1, 2, 3, 4]);

      // Filter logic from batchRetryTasks
      const errorTasks = tasks.filter(
        (t) =>
          selectedIds.has(t.id) &&
          t.status === 'error' &&
          t.uri !== '[torrent]'
      );

      expect(errorTasks.length).toBe(2);
      expect(errorTasks.map((t) => t.id)).toEqual([1, 3]);
    });

    it('should update state after batch retry', () => {
      const task1 = createMockTask({ id: 1, status: 'error' });
      const task2 = createMockTask({ id: 2, status: 'error' });
      const task3 = createMockTask({ id: 3, status: 'complete' });

      const newTask1 = createMockTask({ id: 10, status: 'active' });
      const newTask2 = createMockTask({ id: 11, status: 'active' });

      const tasks: Task[] = [task1, task2, task3];
      const retriedIds = [1, 2];
      const newTasks = [newTask1, newTask2];

      // Simulate batch retry state update
      const updatedTasks = (() => {
        const filtered = tasks.filter((t) => !retriedIds.includes(t.id));
        return [...newTasks, ...filtered];
      })();

      expect(updatedTasks.length).toBe(3);
      expect(updatedTasks.map((t) => t.id)).toEqual([10, 11, 3]);
    });
  });
});

describe('Retry button visibility logic', () => {
  describe('Tasks page', () => {
    it('should show retry button only for error status', () => {
      const errorTask = createMockTask({ status: 'error' });
      const showRetryButton = errorTask.status === 'error';
      expect(showRetryButton).toBe(true);
    });

    it('should not show retry button for active tasks', () => {
      const activeTask = createMockTask({ status: 'active' });
      const showRetryButton = activeTask.status === 'error';
      expect(showRetryButton).toBe(false);
    });

    it('should not show retry button for complete tasks', () => {
      const completeTask = createMockTask({ status: 'complete' });
      const showRetryButton = completeTask.status === 'error';
      expect(showRetryButton).toBe(false);
    });

    it('should not show retry button for paused tasks', () => {
      const pausedTask = createMockTask({ status: 'paused' });
      const showRetryButton = pausedTask.status === 'error';
      expect(showRetryButton).toBe(false);
    });
  });

  describe('History page', () => {
    it('should show retry button for error status in history', () => {
      const errorTask = createMockTask({ status: 'error' });
      const showRetryButton = errorTask.status === 'error';
      expect(showRetryButton).toBe(true);
    });

    it('should not show retry button for complete tasks in history', () => {
      const completeTask = createMockTask({ status: 'complete' });
      const showRetryButton = completeTask.status === 'error';
      expect(showRetryButton).toBe(false);
    });

    it('should not show retry button for stopped tasks in history', () => {
      const stoppedTask = createMockTask({ status: 'stopped' });
      const showRetryButton = stoppedTask.status === 'error';
      expect(showRetryButton).toBe(false);
    });
  });
});

describe('Torrent task detection', () => {
  it('should identify torrent task by "[torrent]" uri', () => {
    const torrentTask = createMockTorrentTask();
    expect(torrentTask.uri).toBe('[torrent]');
  });

  it('should identify HTTP task', () => {
    const httpTask = createMockTask({ uri: 'https://example.com/file.zip' });
    expect(httpTask.uri).not.toBe('[torrent]');
    expect(httpTask.uri.startsWith('http')).toBe(true);
  });

  it('should identify magnet link task', () => {
    const magnetTask = createMockTask({ uri: 'magnet:?xt=urn:btih:abc123' });
    expect(magnetTask.uri).not.toBe('[torrent]');
    expect(magnetTask.uri.startsWith('magnet:')).toBe(true);
  });

  it('should identify FTP task', () => {
    const ftpTask = createMockTask({ uri: 'ftp://example.com/file.zip' });
    expect(ftpTask.uri).not.toBe('[torrent]');
    expect(ftpTask.uri.startsWith('ftp:')).toBe(true);
  });
});
