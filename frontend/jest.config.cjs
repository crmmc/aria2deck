const nextJest = require('next/jest');

const createJestConfig = nextJest({
  // Provide the path to your Next.js app to load next.config.js and .env files
  dir: './',
});

// Add any custom config to be passed to Jest
const config = {
  coverageProvider: 'v8',
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
  collectCoverageFrom: [
    'app/s/[code]/SharePageClient.tsx',
    'app/login/page.tsx',
    'app/(authenticated)/files/page.tsx',
    'app/(authenticated)/history/page.tsx',
    'app/(authenticated)/profile/page.tsx',
    'app/(authenticated)/settings/page.tsx',
    'app/(authenticated)/shares/page.tsx',
    'app/(authenticated)/storage/page.tsx',
    'app/(authenticated)/tasks/page.tsx',
    'app/(authenticated)/users/page.tsx',
    'components/{CreateShareDialog,PackTaskCard,Toast}.tsx',
    'hooks/useTaskWebSocket.ts',
    'lib/**/*.{ts,tsx}',
    '!**/*.d.ts',
  ],
  coverageThreshold: {
    global: {
      branches: 75,
      lines: 75,
      statements: 75,
    },
  },
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/$1',
  },
  testPathIgnorePatterns: ['<rootDir>/node_modules/', '<rootDir>/.next/'],
};

// createJestConfig is exported this way to ensure that next/jest can load the Next.js config which is async
module.exports = createJestConfig(config);
