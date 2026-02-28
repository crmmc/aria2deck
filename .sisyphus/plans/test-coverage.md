# Backend Test Coverage Plan

## TL;DR

> **Quick Summary**: Write comprehensive tests for 10 low-coverage backend modules to achieve >80% test coverage (currently 55%)
> 
> **Deliverables**:
> - 10 new test files covering all low-coverage modules
> - Unit tests for pure functions
> - Integration tests for API endpoints
> - Mocking patterns for external dependencies
> 
> **Estimated Effort**: Large (10 test files, ~2000 lines of test code)
> **Parallel Execution**: YES - 4 waves
> **Critical Path**: Wave 1 → Wave 2 → Wave 3 → Wave 4

---

## Context

### Original Request
Achieve >80% backend test coverage by writing tests for 10 low-coverage modules. Current coverage is 55%.

### Interview Summary
**Key Discussions**:
- Target modules identified with specific coverage percentages
- Existing test patterns from conftest.py analyzed
- Parallel execution strategy designed in 4 waves

**Research Findings**:
- `aria2_rpc_handler.py` has 31 handlers (18 full + 13 silent)
- `hash.py` is pure Python with no external dependencies - easiest to test
- Existing fixtures: `temp_db`, `test_user`, `test_admin`, `user_session`, `authenticated_client`, `mock_aria2_client`
- pytest-asyncio with `asyncio_mode="auto"` - no decorators needed

---

## Work Objectives

### Core Objective
Write comprehensive tests for 10 low-coverage modules to achieve >80% backend test coverage.

### Concrete Deliverables
- `backend/tests/test_hash.py` - Hash utility tests
- `backend/tests/test_http_probe.py` - HTTP probe tests
- `backend/tests/test_history_router.py` - History endpoint tests
- `backend/tests/test_stats_router.py` - Stats endpoint tests
- `backend/tests/test_config_router.py` - Config endpoint tests
- `backend/tests/test_users_router.py` - Users endpoint tests
- `backend/tests/test_ws_router.py` - WebSocket tests
- `backend/tests/test_aria2_rpc_router.py` - JSON-RPC proxy tests
- `backend/tests/test_aria2_client.py` - aria2 client tests
- `backend/tests/test_aria2_rpc_handler_full.py` - RPC handler tests

### Definition of Done
- [ ] `cd backend && uv run pytest --cov=app --cov-report=term-missing` shows >80% coverage
- [ ] All 257+ existing tests still pass
- [ ] No test modifications to existing tests (only additions)

### Must Have
- Tests follow existing patterns from conftest.py
- Class-based test organization
- AsyncMock for async functions
- Proper fixture usage (temp_db, test_user, etc.)

### Must NOT Have (Guardrails)
- No modifications to existing test files
- No `as any` or type suppressions
- No skipped tests without documented reason
- No tests that require real aria2 instance
- No tests that modify production database

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: YES
- **User wants tests**: YES (TDD not required - tests after implementation)
- **Framework**: pytest + pytest-asyncio + pytest-cov

### Automated Verification

Each TODO includes verification via pytest:

```bash
# Run specific test file
cd backend && uv run pytest tests/test_xxx.py -v

# Run with coverage for specific module
cd backend && uv run pytest --cov=app/services/xxx --cov-report=term-missing tests/test_xxx.py

# Run all tests with coverage
cd backend && uv run pytest --cov=app --cov-report=term-missing
```

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately - Easy Wins):
├── Task 1: test_hash.py (pure functions, no deps)
├── Task 2: test_http_probe.py (mock aiohttp)
├── Task 3: test_history_router.py (simple CRUD)
└── Task 4: test_stats_router.py (mock disk_usage)

Wave 2 (After Wave 1 - Medium Complexity):
├── Task 5: test_config_router.py (admin + tokens)
├── Task 6: test_users_router.py (user CRUD)
└── Task 7: test_ws_router.py (WebSocket)

Wave 3 (After Wave 2 - Complex):
├── Task 8: test_aria2_rpc_router.py (JSON-RPC proxy)
└── Task 9: test_aria2_client.py (RPC client)

Wave 4 (After Wave 3 - Most Complex):
└── Task 10: test_aria2_rpc_handler_full.py (31 handlers)

Critical Path: Task 1 → Task 5 → Task 8 → Task 10
Parallel Speedup: ~60% faster than sequential
```

### Dependency Matrix

| Task | Depends On | Blocks | Can Parallelize With |
|------|------------|--------|---------------------|
| 1 | None | 5, 8, 10 | 2, 3, 4 |
| 2 | None | 5, 8, 10 | 1, 3, 4 |
| 3 | None | 5, 8, 10 | 1, 2, 4 |
| 4 | None | 5, 8, 10 | 1, 2, 3 |
| 5 | 1-4 | 8, 10 | 6, 7 |
| 6 | 1-4 | 8, 10 | 5, 7 |
| 7 | 1-4 | 8, 10 | 5, 6 |
| 8 | 5-7 | 10 | 9 |
| 9 | 5-7 | 10 | 8 |
| 10 | 8, 9 | None | None (final) |

---

## TODOs

### Wave 1: Easy Wins (No Dependencies)

- [ ] 1. Create test_hash.py - Hash utility tests

  **What to do**:
  - Test `extract_info_hash_from_magnet()` with valid hex and base32 hashes
  - Test `extract_info_hash_from_torrent()` with valid torrent bytes
  - Test `extract_info_hash_from_torrent_base64()` with base64 encoded torrents
  - Test `calculate_url_hash()` with various URL formats
  - Test `calculate_file_content_hash()` with temp files
  - Test `calculate_directory_content_hash()` with temp directories
  - Test `is_magnet_link()` and `is_http_url()` type checks
  - Test edge cases: malformed magnets, corrupted torrents, empty files

  **Must NOT do**:
  - Do not test with real torrent files from internet
  - Do not create files outside temp directory

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Pure function tests, no external dependencies, straightforward implementation
  - **Skills**: [`python-dev`]
    - `python-dev`: Python testing patterns, pytest conventions

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 2, 3, 4)
  - **Blocks**: Tasks 5, 6, 7, 8, 9, 10
  - **Blocked By**: None (can start immediately)

  **References**:
  - `backend/app/services/hash.py:1-293` - Full implementation to test
  - `backend/tests/conftest.py:39-73` - temp_db fixture pattern for temp files
  - `backend/tests/test_security_utils.py` - Example of testing utility functions

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_hash.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/services/hash --cov-report=term-missing tests/test_hash.py
  # Assert: Coverage >= 85%
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `test(hash): add comprehensive tests for hash utilities`
  - Files: `backend/tests/test_hash.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_hash.py`

---

- [ ] 2. Create test_http_probe.py - HTTP probe tests

  **What to do**:
  - Test `_parse_content_disposition()` with various header formats
  - Test `_extract_filename_from_url()` with different URL patterns
  - Test `probe_http_url()` with mocked aiohttp responses
  - Test `probe_url_with_get_fallback()` when HEAD fails
  - Test error scenarios: timeouts, connection errors, HTTP errors
  - Test redirect handling

  **Must NOT do**:
  - Do not make real HTTP requests
  - Do not test with external URLs

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Focused module with clear mocking requirements
  - **Skills**: [`python-dev`]
    - `python-dev`: aiohttp mocking patterns, async testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 3, 4)
  - **Blocks**: Tasks 5, 6, 7, 8, 9, 10
  - **Blocked By**: None (can start immediately)

  **References**:
  - `backend/app/services/http_probe.py:1-272` - Full implementation
  - `backend/tests/test_ssrf_protection.py` - Example of mocking HTTP requests
  - `backend/tests/conftest.py:100-115` - AsyncMock patterns

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_http_probe.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/services/http_probe --cov-report=term-missing tests/test_http_probe.py
  # Assert: Coverage >= 80%
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `test(http_probe): add tests for HTTP URL probing`
  - Files: `backend/tests/test_http_probe.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_http_probe.py`

---

- [ ] 3. Create test_history_router.py - History endpoint tests

  **What to do**:
  - Test `GET /api/history` returns user's history
  - Test `DELETE /api/history/{id}` deletes single record
  - Test `DELETE /api/history` clears all history
  - Test 404 for non-existent history
  - Test user isolation (can't see other user's history)
  - Test authentication required

  **Must NOT do**:
  - Do not modify existing test files
  - Do not test without temp_db fixture

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Simple CRUD endpoints, existing fixture patterns
  - **Skills**: [`python-dev`]
    - `python-dev`: FastAPI testing, TestClient usage

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 4)
  - **Blocks**: Tasks 5, 6, 7, 8, 9, 10
  - **Blocked By**: None (can start immediately)

  **References**:
  - `backend/app/routers/history.py:1-81` - Full implementation
  - `backend/tests/conftest.py:75-98` - test_user, user_session fixtures
  - `backend/tests/test_delete_user_cleanup.py` - Example of testing with fixtures

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_history_router.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/routers/history --cov-report=term-missing tests/test_history_router.py
  # Assert: Coverage >= 85%
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `test(history): add tests for history endpoints`
  - Files: `backend/tests/test_history_router.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_history_router.py`

---

- [ ] 4. Create test_stats_router.py - Stats endpoint tests

  **What to do**:
  - Test `GET /api/stats` returns user stats
  - Test `GET /api/stats/machine` returns machine stats (admin only)
  - Test space calculation with mocked disk_usage
  - Test quota logic (user quota vs machine free)
  - Test active task counting
  - Test admin-only access control

  **Must NOT do**:
  - Do not use real disk_usage (mock it)
  - Do not test without temp_db fixture

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Two endpoints, clear mocking requirements
  - **Skills**: [`python-dev`]
    - `python-dev`: shutil.disk_usage mocking, FastAPI testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 3)
  - **Blocks**: Tasks 5, 6, 7, 8, 9, 10
  - **Blocked By**: None (can start immediately)

  **References**:
  - `backend/app/routers/stats.py:1-121` - Full implementation
  - `backend/tests/conftest.py:75-98` - test_user, test_admin fixtures
  - `backend/tests/test_pack.py:50-80` - Example of mocking disk_usage

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_stats_router.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/routers/stats --cov-report=term-missing tests/test_stats_router.py
  # Assert: Coverage >= 85%
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `test(stats): add tests for stats endpoints`
  - Files: `backend/tests/test_stats_router.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_stats_router.py`

---

### Wave 2: Medium Complexity (After Wave 1)

- [ ] 5. Create test_config_router.py - Config endpoint tests

  **What to do**:
  - Test `GET /api/config` returns config (admin only)
  - Test `PUT /api/config` updates config (admin only)
  - Test `GET /api/config/aria2/version` returns version
  - Test `POST /api/config/aria2/test` tests connection
  - Test token CRUD: GET/POST/DELETE /api/config/tokens
  - Test rate limiting on aria2/test endpoint
  - Test config value clamping (min/max ranges)
  - Test secret masking in responses

  **Must NOT do**:
  - Do not connect to real aria2
  - Do not modify existing config tests

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Multiple endpoints but clear patterns
  - **Skills**: [`python-dev`]
    - `python-dev`: Admin auth testing, rate limit testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 6, 7)
  - **Blocks**: Tasks 8, 9, 10
  - **Blocked By**: Tasks 1, 2, 3, 4

  **References**:
  - `backend/app/routers/config.py:1-532` - Full implementation
  - `backend/tests/conftest.py:100-115` - mock_aria2_client fixture
  - `backend/tests/test_rate_limit.py` - Rate limiting test patterns

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_config_router.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/routers/config --cov-report=term-missing tests/test_config_router.py
  # Assert: Coverage >= 80%
  ```

  **Commit**: YES (groups with Wave 2)
  - Message: `test(config): add tests for config and token endpoints`
  - Files: `backend/tests/test_config_router.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_config_router.py`

---

- [ ] 6. Create test_users_router.py - Users endpoint tests

  **What to do**:
  - Test `POST /api/users` creates user (first user no auth, then admin)
  - Test `GET /api/users` lists users (admin only)
  - Test `GET/PUT/DELETE /api/users/{id}` CRUD (admin only)
  - Test RPC access endpoints: GET/PUT/POST /api/users/me/rpc-access
  - Test first-user creation race condition
  - Test cascade delete (sessions, tasks, files)
  - Test password change invalidates sessions
  - Test admin cannot delete self

  **Must NOT do**:
  - Do not modify existing user tests
  - Do not test without temp_db fixture

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: CRUD patterns with auth boundaries
  - **Skills**: [`python-dev`]
    - `python-dev`: Auth testing, cascade delete testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 5, 7)
  - **Blocks**: Tasks 8, 9, 10
  - **Blocked By**: Tasks 1, 2, 3, 4

  **References**:
  - `backend/app/routers/users.py:1-395` - Full implementation
  - `backend/tests/test_delete_user_cleanup.py` - Cascade delete patterns
  - `backend/tests/test_initial_password.py` - User creation patterns

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_users_router.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/routers/users --cov-report=term-missing tests/test_users_router.py
  # Assert: Coverage >= 75%
  ```

  **Commit**: YES (groups with Wave 2)
  - Message: `test(users): add tests for user management endpoints`
  - Files: `backend/tests/test_users_router.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_users_router.py`

---

- [ ] 7. Create test_ws_router.py - WebSocket tests

  **What to do**:
  - Test WebSocket connection with valid session
  - Test WebSocket rejection with invalid session (4401 code)
  - Test heartbeat mechanism
  - Test connection cleanup on disconnect
  - Test ping/pong handling

  **Must NOT do**:
  - Do not test real WebSocket connections
  - Do not modify existing WebSocket tests

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Single endpoint, focused testing
  - **Skills**: [`python-dev`]
    - `python-dev`: WebSocket testing with TestClient

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 5, 6)
  - **Blocks**: Tasks 8, 9, 10
  - **Blocked By**: Tasks 1, 2, 3, 4

  **References**:
  - `backend/app/routers/ws.py:1-60` - Full implementation
  - `backend/tests/test_websocket_race.py` - WebSocket testing patterns
  - `backend/tests/conftest.py:75-98` - Session fixtures

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_ws_router.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/routers/ws --cov-report=term-missing tests/test_ws_router.py
  # Assert: Coverage >= 80%
  ```

  **Commit**: YES (groups with Wave 2)
  - Message: `test(ws): add tests for WebSocket endpoint`
  - Files: `backend/tests/test_ws_router.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_ws_router.py`

---

### Wave 3: Complex (After Wave 2)

- [ ] 8. Create test_aria2_rpc_router.py - JSON-RPC proxy tests

  **What to do**:
  - Test single JSON-RPC request handling
  - Test batch JSON-RPC request handling
  - Test token extraction from params
  - Test rate limiting (100 req/60s per IP)
  - Test error responses: parse error, invalid request, invalid params
  - Test unauthorized access (invalid token)
  - Test user isolation via RPC secret

  **Must NOT do**:
  - Do not connect to real aria2
  - Do not test actual RPC handler logic (separate test file)

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Single endpoint with clear mocking
  - **Skills**: [`python-dev`]
    - `python-dev`: JSON-RPC testing, rate limit testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Task 9)
  - **Blocks**: Task 10
  - **Blocked By**: Tasks 5, 6, 7

  **References**:
  - `backend/app/routers/aria2_rpc.py:1-336` - Full implementation
  - `backend/tests/test_rate_limit.py` - Rate limiting patterns
  - `backend/tests/conftest.py:100-115` - mock_aria2_client fixture

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_aria2_rpc_router.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/routers/aria2_rpc --cov-report=term-missing tests/test_aria2_rpc_router.py
  # Assert: Coverage >= 75%
  ```

  **Commit**: YES (groups with Wave 3)
  - Message: `test(aria2_rpc): add tests for JSON-RPC proxy endpoint`
  - Files: `backend/tests/test_aria2_rpc_router.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_aria2_rpc_router.py`

---

- [ ] 9. Create test_aria2_client.py - aria2 client tests

  **What to do**:
  - Test `_call()` method with mocked aiohttp
  - Test `add_uri()`, `add_torrent()` methods
  - Test `tell_status()`, `tell_active()`, `tell_waiting()`, `tell_stopped()`
  - Test `pause()`, `unpause()`, `remove()`, `force_remove()`
  - Test `get_files()`, `get_version()`, `change_position()`
  - Test error handling for RPC failures
  - Test connection timeout handling

  **Must NOT do**:
  - Do not connect to real aria2
  - Do not test with real network

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: RPC client with clear mocking
  - **Skills**: [`python-dev`]
    - `python-dev`: aiohttp mocking, async testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Task 8)
  - **Blocks**: Task 10
  - **Blocked By**: Tasks 5, 6, 7

  **References**:
  - `backend/app/aria2/client.py:1-106` - Full implementation
  - `backend/tests/conftest.py:100-115` - mock_aria2_client fixture
  - `backend/tests/test_aria2_errors.py` - aria2 error handling patterns

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_aria2_client.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/aria2/client --cov-report=term-missing tests/test_aria2_client.py
  # Assert: Coverage >= 80%
  ```

  **Commit**: YES (groups with Wave 3)
  - Message: `test(aria2_client): add tests for aria2 RPC client`
  - Files: `backend/tests/test_aria2_client.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_aria2_client.py`

---

### Wave 4: Most Complex (After Wave 3)

- [ ] 10. Create test_aria2_rpc_handler_full.py - RPC handler tests

  **What to do**:
  - Test `handle()` dispatch logic
  - Test `_get_handler_name()` method name conversion
  - Test `_verify_task_owner()` ownership verification
  - Test `_check_disk_space()` and `_get_user_available_space()`
  - Test `_sanitize_path()` and `_sanitize_status()`
  - Test all 18 full handlers:
    - `_handle_add_uri`, `_handle_add_torrent`
    - `_handle_remove`, `_handle_force_remove`
    - `_handle_pause`, `_handle_force_pause`, `_handle_unpause`
    - `_handle_tell_status`, `_handle_tell_active`, `_handle_tell_waiting`, `_handle_tell_stopped`
    - `_handle_get_files`, `_handle_get_uris`
    - `_handle_get_global_stat`, `_handle_get_version`
    - `_handle_change_position`
    - `_handle_system_list_methods`, `_handle_system_multicall`
  - Test 13 silent handlers return expected values
  - Test quota lock prevents race conditions
  - Test user isolation in query methods

  **Must NOT do**:
  - Do not connect to real aria2
  - Do not modify existing test_aria2_rpc_handler.py

  **Recommended Agent Profile**:
  - **Category**: `ultrabrain`
    - Reason: Most complex module, 31 handlers, requires deep understanding
  - **Skills**: [`python-dev`]
    - `python-dev`: Complex mocking, async testing, race condition testing

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 4 (sequential)
  - **Blocks**: None (final task)
  - **Blocked By**: Tasks 8, 9

  **References**:
  - `backend/app/services/aria2_rpc_handler.py:1-925` - Full implementation
  - `backend/tests/test_aria2_rpc_handler.py` - Existing single test
  - `backend/tests/conftest.py:100-115` - mock_aria2_client fixture
  - `backend/tests/test_space_freeze_race.py` - Quota lock testing patterns

  **Acceptance Criteria**:
  ```bash
  cd backend && uv run pytest tests/test_aria2_rpc_handler_full.py -v
  # Assert: All tests pass
  
  cd backend && uv run pytest --cov=app/services/aria2_rpc_handler --cov-report=term-missing tests/test_aria2_rpc_handler_full.py
  # Assert: Coverage >= 75%
  ```

  **Commit**: YES
  - Message: `test(aria2_rpc_handler): add comprehensive tests for all RPC handlers`
  - Files: `backend/tests/test_aria2_rpc_handler_full.py`
  - Pre-commit: `cd backend && uv run pytest tests/test_aria2_rpc_handler_full.py`

---

## Commit Strategy

| After Task | Message | Files | Verification |
|------------|---------|-------|--------------|
| Wave 1 (1-4) | `test: add tests for hash, http_probe, history, stats` | 4 test files | `uv run pytest tests/test_hash.py tests/test_http_probe.py tests/test_history_router.py tests/test_stats_router.py` |
| Wave 2 (5-7) | `test: add tests for config, users, ws routers` | 3 test files | `uv run pytest tests/test_config_router.py tests/test_users_router.py tests/test_ws_router.py` |
| Wave 3 (8-9) | `test: add tests for aria2 rpc router and client` | 2 test files | `uv run pytest tests/test_aria2_rpc_router.py tests/test_aria2_client.py` |
| Wave 4 (10) | `test: add comprehensive aria2 rpc handler tests` | 1 test file | `uv run pytest tests/test_aria2_rpc_handler_full.py` |

---

## Success Criteria

### Verification Commands
```bash
# Final coverage check
cd backend && uv run pytest --cov=app --cov-report=term-missing
# Expected: Coverage > 80%

# All tests pass
cd backend && uv run pytest
# Expected: 300+ tests pass, 0 failures
```

### Final Checklist
- [ ] All 10 test files created
- [ ] Coverage > 80% achieved
- [ ] All existing 257 tests still pass
- [ ] No modifications to existing test files
- [ ] No skipped tests without documented reason
