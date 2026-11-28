/** @type {import('jest').Config} */
module.exports = {
  coverageProvider: 'v8',
  testEnvironment: 'jsdom',
  // 全量 + 覆盖率插桩在负载机器上的单用例耗时远高于开发机空载；30s 是护栏而非修复，
  // 真正的提速来自 setup.ts 注入原生 webcrypto（消除纯 JS PBKDF2）。
  testTimeout: 30000,
  roots: ['<rootDir>/tests'],
  setupFilesAfterEnv: ['<rootDir>/tests/setup.ts'],
  transform: {
    '^.+\.tsx?$': [
      'ts-jest',
      {
        tsconfig: {
          module: 'CommonJS',
          moduleResolution: 'Node',
          jsx: 'react-jsx',
          target: 'ES2021',
          esModuleInterop: true,
        },
      },
    ],
  },
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
  },
  collectCoverageFrom: [
    'src/**/*.{ts,tsx}',
    '!src/main.tsx',
    '!src/**/*.d.ts',
  ],
  coverageThreshold: {
    global: {
      branches: 95,
      functions: 95,
      lines: 95,
      statements: 95,
    },
  },
  testPathIgnorePatterns: ['/node_modules/', '/dist/'],
};
