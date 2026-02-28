# Frontend Test Implementation Plan

## TL;DR

> **Quick Summary**: Implement comprehensive unit/integration tests for Aria2Deck Next.js frontend to achieve >= 80% code coverage from current 0%.
> 
> **Deliverables**:
> - Test infrastructure setup (missing dependencies)
> - 150+ test cases across 12 test files
> - Mock utilities for browser APIs
> - >= 80% code coverage
> 
> **Estimated Effort**: Large (5-7 hours)
> **Parallel Execution**: YES - 5 waves
> **Critical Path**: Task 1 (deps) → Task 2 (mocks) → Tasks 3-12 (tests)

---

## Context

### Original Request
Implement frontend tests to achieve >= 80% code coverage for the Aria2Deck Next.js frontend application.

### Interview Summary
**Key Discussions**:
- Test framework: Jest + jsdom (already configured)
- Missing: @testing-library/react, @testing-library/user-event
- Approach: Tests-after (testing existing code)
- Location: `__tests__/` directory per existing tsconfig

**Research Findings**:
- jest.setup.ts already mocks next/navigation, alert, confirm
- Large page components (tasks: 888 lines, files: 898 lines) need focused testing
- 30+ API methods in lib/api.ts require comprehensive mocking

---

## Work Objectives

### Core Objective
Achieve >= 80% frontend test coverage through systematic unit and integration testing of all lib/, hooks/, components/, and page files.

### Concrete Deliverables
- `frontend/__tests__/lib/utils.test.ts` - Pure function tests
- `frontend/__tests__/lib/crypto.test.ts` - Crypto function tests
- `frontend/__tests__/lib/notification.test.ts` - Notification tests
- `frontend/__tests__/lib/api.test.ts` - API client tests
- `frontend/__tests__/lib/AuthContext.test.tsx` - Auth context tests
- `frontend/__tests__/hooks/useTaskWebSocket.test.ts` - WebSocket hook tests
- `frontend/__tests__/components/Toast.test.tsx` - Toast component tests
- `frontend/__tests__/components/Sidebar.test.tsx` - Sidebar tests
- `frontend/__tests__/components/StatsWidget.test.tsx` - Stats widget tests
- `frontend/__tests__/pages/login.test.tsx` - Login page tests
- `frontend/__tests__/pages/tasks.test.tsx` - Tasks page tests
- `frontend/__tests__/pages/files.test.tsx` - Files page tests
- `frontend/__tests__/pages/users.test.tsx` - Users page tests
- `frontend/__mocks__/api.ts` - API mock utilities

### Definition of Done
- [ ] `cd frontend && bun run test -- --coverage` shows >= 80% coverage
- [ ] All tests pass (0 failures)
- [ ] No TypeScript errors in test files

### Must Have
- Tests for all exported functions in lib/
- Tests for all custom hooks
- Tests for key user interactions in page components
- Proper mocking of browser APIs (fetch, localStorage, WebSocket, etc.)

### Must NOT Have (Guardrails)
- E2E tests (out of scope - unit/integration only)
- Backend tests (separate project)
- Snapshot tests (brittle, avoid)
- Tests that require real network calls
- Over-mocking that tests implementation details instead of behavior

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: YES (Jest configured)
- **User wants tests**: YES (Tests-after approach)
- **Framework**: Jest + @testing-library/react

### Automated Verification

Each TODO includes executable verification:

```bash
# Run all tests with coverage
cd frontend && bun run test -- --coverage

# Run specific test file
cd frontend && bun run test __tests__/lib/utils.test.ts

# Check coverage threshold
cd frontend && bun run test -- --coverage --coverageThreshold='{"global":{"lines":80}}'
```

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately):
└── Task 1: Install dependencies

Wave 2 (After Wave 1):
└── Task 2: Create mock utilities

Wave 3 (After Wave 2 - PARALLEL):
├── Task 3: lib/utils.ts tests
├── Task 4: lib/crypto.ts tests
└── Task 5: lib/notification.ts tests

Wave 4 (After Wave 3 - PARALLEL):
├── Task 6: lib/api.ts tests
├── Task 7: lib/AuthContext.tsx tests
├── Task 8: hooks/useTaskWebSocket.ts tests
└── Task 9: components/Toast.tsx tests

Wave 5 (After Wave 4 - PARALLEL):
├── Task 10: components/Sidebar.tsx tests
├── Task 11: components/StatsWidget.tsx tests
├── Task 12: pages/login.test.tsx
├── Task 13: pages/tasks.test.tsx
├── Task 14: pages/files.test.tsx
└── Task 15: pages/users.test.tsx

Critical Path: Task 1 → Task 2 → Task 6 → Task 13
```

### Dependency Matrix

| Task | Depends On | Blocks | Can Parallelize With |
|------|------------|--------|---------------------|
| 1 | None | 2-15 | None |
| 2 | 1 | 3-15 | None |
| 3 | 2 | None | 4, 5 |
| 4 | 2 | None | 3, 5 |
| 5 | 2 | None | 3, 4 |
| 6 | 3, 4, 5 | 12-15 | 7, 8, 9 |
| 7 | 3, 4, 5 | 12-15 | 6, 8, 9 |
| 8 | 3, 4, 5 | 13 | 6, 7, 9 |
| 9 | 3, 4, 5 | None | 6, 7, 8 |
| 10 | 6, 7 | None | 11, 12, 13, 14, 15 |
| 11 | 6, 7 | None | 10, 12, 13, 14, 15 |
| 12 | 6, 7 | None | 10, 11, 13, 14, 15 |
| 13 | 6, 7, 8 | None | 10, 11, 12, 14, 15 |
| 14 | 6, 7 | None | 10, 11, 12, 13, 15 |
| 15 | 6, 7 | None | 10, 11, 12, 13, 14 |

---

## TODOs

- [ ] 1. Install Test Dependencies

  **What to do**:
  - Install @testing-library/react and @testing-library/user-event
  - Verify installation by checking package.json

  **Must NOT do**:
  - Modify jest.config.cjs (already configured)
  - Install unnecessary dependencies

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Simple dependency installation, single command
  - **Skills**: None needed
  - **Skills Evaluated but Omitted**:
    - `frontend-dev`: Not needed for npm install

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 1 (sequential start)
  - **Blocks**: Tasks 2-15
  - **Blocked By**: None

  **References**:
  - `frontend/package.json` - Current dependencies list
  - `frontend/jest.config.cjs` - Jest configuration (already has @testing-library setup)

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun add -D @testing-library/react @testing-library/user-event
  # Assert: Exit code 0
  
  cd frontend && bun pm ls | grep -E "@testing-library/(react|user-event)"
  # Assert: Both packages listed
  ```

  **Commit**: YES
  - Message: `chore(frontend): add testing-library dependencies`
  - Files: `frontend/package.json`, `frontend/bun.lockb`
  - Pre-commit: None

---

- [ ] 2. Create Mock Utilities

  **What to do**:
  - Create `__mocks__/api.ts` with mocked API functions
  - Add WebSocket mock utility
  - Add localStorage mock utility
  - Add Web Crypto API mock

  **Must NOT do**:
  - Mock next/navigation (already done in jest.setup.ts)
  - Create overly complex mock implementations

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Utility file creation, straightforward patterns
  - **Skills**: [`frontend-dev`]
    - `frontend-dev`: TypeScript patterns for mocking

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 2 (after deps)
  - **Blocks**: Tasks 3-15
  - **Blocked By**: Task 1

  **References**:
  **Pattern References**:
  - `frontend/lib/api.ts:1-50` - ApiError class and request function signature
  - `frontend/jest.setup.ts` - Existing mock patterns for next/navigation

  **API/Type References**:
  - `frontend/lib/api.ts:ApiError` - Error class to mock
  - `frontend/types.ts` - Type definitions for mock return values

  **Acceptance Criteria**:
  ```bash
  # Verify mock file exists and is valid TypeScript
  cd frontend && bun run tsc --noEmit __mocks__/api.ts 2>/dev/null || echo "Checking..."
  # Assert: No TypeScript errors
  
  # Verify mock exports expected functions
  cd frontend && grep -E "export (const|function)" __mocks__/api.ts | wc -l
  # Assert: >= 5 exports
  ```

  **Commit**: YES
  - Message: `test(frontend): add mock utilities for API and browser APIs`
  - Files: `frontend/__mocks__/api.ts`
  - Pre-commit: `bun run tsc --noEmit`

---

- [ ] 3. Test lib/utils.ts

  **What to do**:
  - Test `bytesToGB` function with various inputs (0, small, large, edge cases)
  - Test `gbToBytes` function (inverse of bytesToGB)
  - Test `formatBytes` function with different units (B, KB, MB, GB, TB)
  - Test edge cases: negative numbers, NaN, Infinity

  **Must NOT do**:
  - Test internal implementation details
  - Add unnecessary type assertions

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Pure functions, no mocking needed, straightforward tests
  - **Skills**: None needed

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Tasks 4, 5)
  - **Blocks**: None
  - **Blocked By**: Task 2

  **References**:
  **Pattern References**:
  - `frontend/lib/utils.ts:1-29` - All three functions to test

  **Test References**:
  - `frontend/jest.setup.ts` - Test setup patterns

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/lib/utils.test.ts --coverage
  # Assert: All tests pass
  # Assert: lib/utils.ts coverage >= 95%
  ```

  **Commit**: NO (groups with Task 5)

---

- [ ] 4. Test lib/crypto.ts

  **What to do**:
  - Mock Web Crypto API (crypto.subtle.importKey, deriveBits)
  - Test `hashPassword` with valid inputs
  - Test `hashPassword` with empty password
  - Test `hashPassword` with special characters
  - Verify output format (hex string)

  **Must NOT do**:
  - Test actual cryptographic correctness (mock handles that)
  - Use real crypto operations in tests

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Single async function with mocked crypto
  - **Skills**: None needed

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Tasks 3, 5)
  - **Blocks**: None
  - **Blocked By**: Task 2

  **References**:
  **Pattern References**:
  - `frontend/lib/crypto.ts:1-48` - hashPassword function implementation

  **External References**:
  - Web Crypto API: https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/lib/crypto.test.ts --coverage
  # Assert: All tests pass
  # Assert: lib/crypto.ts coverage >= 90%
  ```

  **Commit**: NO (groups with Task 5)

---

- [ ] 5. Test lib/notification.ts

  **What to do**:
  - Mock localStorage for settings persistence
  - Mock Notification API for browser notifications
  - Mock Audio for sound notifications
  - Test `getNotificationSettings` returns defaults when empty
  - Test `saveNotificationSettings` persists to localStorage
  - Test `requestNotificationPermission` handles granted/denied/default
  - Test `showNotification` creates notification when enabled
  - Test `playNotificationSound` plays audio when enabled

  **Must NOT do**:
  - Test actual browser notification display
  - Test actual audio playback

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Multiple small functions with browser API mocks
  - **Skills**: [`frontend-dev`]
    - `frontend-dev`: Browser API mocking patterns

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Tasks 3, 4)
  - **Blocks**: None
  - **Blocked By**: Task 2

  **References**:
  **Pattern References**:
  - `frontend/lib/notification.ts:1-87` - All notification functions

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/lib/notification.test.ts --coverage
  # Assert: All tests pass
  # Assert: lib/notification.ts coverage >= 90%
  ```

  **Commit**: YES
  - Message: `test(frontend): add tests for utils, crypto, and notification libs`
  - Files: `frontend/__tests__/lib/utils.test.ts`, `frontend/__tests__/lib/crypto.test.ts`, `frontend/__tests__/lib/notification.test.ts`
  - Pre-commit: `bun run test __tests__/lib/`

---

- [ ] 6. Test lib/api.ts

  **What to do**:
  - Mock global fetch
  - Test `ApiError` class construction and properties
  - Test `request` function with successful responses
  - Test `request` function with error responses (401, 403, 404, 500)
  - Test `authEvents` dispatches on 401
  - Test key API methods: `login`, `logout`, `getCurrentUser`, `getTasks`, `createTask`, `getFiles`, `deleteFile`
  - Test request headers (Content-Type, Authorization)

  **Must NOT do**:
  - Test all 30+ API methods (focus on patterns, not exhaustive)
  - Make real network requests

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Complex module with many methods, requires careful mocking
  - **Skills**: [`frontend-dev`]
    - `frontend-dev`: Fetch mocking, async testing patterns

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 7, 8, 9)
  - **Blocks**: Tasks 10-15
  - **Blocked By**: Tasks 3, 4, 5

  **References**:
  **Pattern References**:
  - `frontend/lib/api.ts:1-242` - Full API client implementation
  - `frontend/lib/api.ts:1-30` - ApiError class and authEvents

  **API/Type References**:
  - `frontend/types.ts:User` - User type for login response
  - `frontend/types.ts:Task` - Task type for task responses

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/lib/api.test.ts --coverage
  # Assert: All tests pass
  # Assert: lib/api.ts coverage >= 80%
  ```

  **Commit**: NO (groups with Task 9)

---

- [ ] 7. Test lib/AuthContext.tsx

  **What to do**:
  - Test `AuthProvider` renders children
  - Test `useAuth` returns context values
  - Test `login` updates user state and localStorage
  - Test `logout` clears user state and localStorage
  - Test `updateUser` updates user state
  - Test context listens to `authEvents` for forced logout
  - Test initial state loads from localStorage

  **Must NOT do**:
  - Test internal React implementation details
  - Test localStorage directly (mock it)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: React context testing requires renderHook and proper setup
  - **Skills**: [`frontend-dev`]
    - `frontend-dev`: React context testing patterns

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 6, 8, 9)
  - **Blocks**: Tasks 10-15
  - **Blocked By**: Tasks 3, 4, 5

  **References**:
  **Pattern References**:
  - `frontend/lib/AuthContext.tsx:1-138` - Full context implementation
  - `frontend/lib/api.ts:authEvents` - Event target for logout events

  **Test References**:
  - @testing-library/react: `renderHook`, `act` for hook testing

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/lib/AuthContext.test.tsx --coverage
  # Assert: All tests pass
  # Assert: lib/AuthContext.tsx coverage >= 85%
  ```

  **Commit**: NO (groups with Task 9)

---

- [ ] 8. Test hooks/useTaskWebSocket.ts

  **What to do**:
  - Mock WebSocket constructor
  - Test hook connects to correct URL
  - Test hook handles incoming messages
  - Test hook handles connection errors
  - Test hook handles reconnection with exponential backoff
  - Test hook cleans up on unmount

  **Must NOT do**:
  - Test actual WebSocket connections
  - Test timing-dependent behavior without fake timers

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: WebSocket mocking and timer handling is complex
  - **Skills**: [`frontend-dev`]
    - `frontend-dev`: WebSocket mocking, fake timers

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 6, 7, 9)
  - **Blocks**: Task 13
  - **Blocked By**: Tasks 3, 4, 5

  **References**:
  **Pattern References**:
  - `frontend/hooks/useTaskWebSocket.ts:1-95` - Full hook implementation

  **External References**:
  - Jest fake timers: https://jestjs.io/docs/timer-mocks

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/hooks/useTaskWebSocket.test.ts --coverage
  # Assert: All tests pass
  # Assert: hooks/useTaskWebSocket.ts coverage >= 80%
  ```

  **Commit**: NO (groups with Task 9)

---

- [ ] 9. Test components/Toast.tsx

  **What to do**:
  - Mock createPortal from react-dom
  - Test `ToastProvider` renders children
  - Test `useToast` returns toast functions
  - Test `showToast` displays toast message
  - Test toast auto-dismisses after timeout
  - Test multiple toasts stack correctly
  - Test toast types (success, error, warning, info)

  **Must NOT do**:
  - Test CSS animations
  - Test portal rendering details

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Component testing with standard patterns
  - **Skills**: [`frontend-dev`]
    - `frontend-dev`: React component testing

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 6, 7, 8)
  - **Blocks**: None
  - **Blocked By**: Tasks 3, 4, 5

  **References**:
  **Pattern References**:
  - `frontend/components/Toast.tsx:1-163` - Full component implementation

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/components/Toast.test.tsx --coverage
  # Assert: All tests pass
  # Assert: components/Toast.tsx coverage >= 85%
  ```

  **Commit**: YES
  - Message: `test(frontend): add tests for api, AuthContext, WebSocket hook, and Toast`
  - Files: `frontend/__tests__/lib/api.test.ts`, `frontend/__tests__/lib/AuthContext.test.tsx`, `frontend/__tests__/hooks/useTaskWebSocket.test.ts`, `frontend/__tests__/components/Toast.test.tsx`
  - Pre-commit: `bun run test`

---

- [ ] 10. Test components/Sidebar.tsx

  **What to do**:
  - Test sidebar renders navigation links
  - Test active link highlighting
  - Test logout button calls logout function
  - Test admin-only links visibility

  **Must NOT do**:
  - Test CSS styling
  - Test router navigation (mocked)

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Simple component with navigation links
  - **Skills**: [`frontend-dev`]

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 (with Tasks 11-15)
  - **Blocks**: None
  - **Blocked By**: Tasks 6, 7

  **References**:
  **Pattern References**:
  - `frontend/components/Sidebar.tsx` - Sidebar component

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/components/Sidebar.test.tsx --coverage
  # Assert: All tests pass
  ```

  **Commit**: NO (groups with Task 15)

---

- [ ] 11. Test components/StatsWidget.tsx

  **What to do**:
  - Test widget renders with stats data
  - Test widget handles loading state
  - Test widget handles error state
  - Test space usage display (used/frozen/available)

  **Must NOT do**:
  - Test CSS styling
  - Test actual API calls

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Simple display component
  - **Skills**: [`frontend-dev`]

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 (with Tasks 10, 12-15)
  - **Blocks**: None
  - **Blocked By**: Tasks 6, 7

  **References**:
  **Pattern References**:
  - `frontend/components/StatsWidget.tsx` - Stats widget component

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/components/StatsWidget.test.tsx --coverage
  # Assert: All tests pass
  ```

  **Commit**: NO (groups with Task 15)

---

- [ ] 12. Test pages/login

  **What to do**:
  - Test login form renders
  - Test form validation (empty fields)
  - Test successful login redirects
  - Test failed login shows error
  - Test password visibility toggle

  **Must NOT do**:
  - Test actual authentication
  - Test router navigation implementation

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Simple form component
  - **Skills**: [`frontend-dev`]

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 (with Tasks 10, 11, 13-15)
  - **Blocks**: None
  - **Blocked By**: Tasks 6, 7

  **References**:
  **Pattern References**:
  - `frontend/app/login/page.tsx:1-87` - Login page component

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/pages/login.test.tsx --coverage
  # Assert: All tests pass
  ```

  **Commit**: NO (groups with Task 15)

---

- [ ] 13. Test pages/tasks

  **What to do**:
  - Test tasks page renders task list
  - Test task creation form
  - Test task status filtering
  - Test task deletion
  - Test WebSocket updates reflect in UI
  - Test empty state display

  **Must NOT do**:
  - Test all 888 lines exhaustively
  - Test CSS animations
  - Test actual WebSocket connections

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Large complex component with many interactions
  - **Skills**: [`frontend-dev`]

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 (with Tasks 10-12, 14, 15)
  - **Blocks**: None
  - **Blocked By**: Tasks 6, 7, 8

  **References**:
  **Pattern References**:
  - `frontend/app/(authenticated)/tasks/page.tsx:1-100` - Component structure and state
  - `frontend/app/(authenticated)/tasks/page.tsx:100-200` - Task list rendering
  - `frontend/app/(authenticated)/tasks/page.tsx:200-300` - Task creation

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/pages/tasks.test.tsx --coverage
  # Assert: All tests pass
  # Assert: Key user flows covered
  ```

  **Commit**: NO (groups with Task 15)

---

- [ ] 14. Test pages/files

  **What to do**:
  - Test files page renders file list
  - Test file download action
  - Test file deletion with confirmation
  - Test file rename functionality
  - Test folder browsing for BT files
  - Test empty state display

  **Must NOT do**:
  - Test all 898 lines exhaustively
  - Test actual file downloads
  - Test CSS styling

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Large complex component with many interactions
  - **Skills**: [`frontend-dev`]

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 (with Tasks 10-13, 15)
  - **Blocks**: None
  - **Blocked By**: Tasks 6, 7

  **References**:
  **Pattern References**:
  - `frontend/app/(authenticated)/files/page.tsx:1-100` - Component structure
  - `frontend/app/(authenticated)/files/page.tsx:100-200` - File list rendering

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/pages/files.test.tsx --coverage
  # Assert: All tests pass
  # Assert: Key user flows covered
  ```

  **Commit**: NO (groups with Task 15)

---

- [ ] 15. Test pages/users

  **What to do**:
  - Test users page renders user list (admin only)
  - Test user creation form
  - Test user editing
  - Test user deletion with confirmation
  - Test quota management
  - Test non-admin redirect

  **Must NOT do**:
  - Test actual user management API calls
  - Test CSS styling

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Medium complexity admin page
  - **Skills**: [`frontend-dev`]

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 5 (with Tasks 10-14)
  - **Blocks**: None
  - **Blocked By**: Tasks 6, 7

  **References**:
  **Pattern References**:
  - `frontend/app/(authenticated)/users/page.tsx:1-469` - Users page component

  **Acceptance Criteria**:
  ```bash
  cd frontend && bun run test __tests__/pages/users.test.tsx --coverage
  # Assert: All tests pass
  ```

  **Commit**: YES
  - Message: `test(frontend): add tests for Sidebar, StatsWidget, and all page components`
  - Files: `frontend/__tests__/components/Sidebar.test.tsx`, `frontend/__tests__/components/StatsWidget.test.tsx`, `frontend/__tests__/pages/*.test.tsx`
  - Pre-commit: `bun run test -- --coverage`

---

## Commit Strategy

| After Task | Message | Files | Verification |
|------------|---------|-------|--------------|
| 1 | `chore(frontend): add testing-library dependencies` | package.json, bun.lockb | bun pm ls |
| 2 | `test(frontend): add mock utilities` | __mocks__/api.ts | tsc --noEmit |
| 5 | `test(frontend): add lib tests (utils, crypto, notification)` | __tests__/lib/*.test.ts | bun run test |
| 9 | `test(frontend): add api, context, hook, and Toast tests` | __tests__/lib/api.test.ts, etc. | bun run test |
| 15 | `test(frontend): add component and page tests` | __tests__/components/*, __tests__/pages/* | bun run test --coverage |

---

## Success Criteria

### Verification Commands
```bash
# Full test suite with coverage
cd frontend && bun run test -- --coverage
# Expected: All tests pass, >= 80% coverage

# Coverage threshold check
cd frontend && bun run test -- --coverage --coverageThreshold='{"global":{"lines":80,"branches":70,"functions":80,"statements":80}}'
# Expected: Exit code 0
```

### Final Checklist
- [ ] All test files created in `__tests__/` directory
- [ ] All tests pass (0 failures)
- [ ] Coverage >= 80% for lines, functions, statements
- [ ] Coverage >= 70% for branches
- [ ] No TypeScript errors in test files
- [ ] Mock utilities properly isolate tests from external dependencies
- [ ] No snapshot tests (per guardrails)
- [ ] No E2E tests (per scope)
