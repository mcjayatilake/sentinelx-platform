# SentinelX Frontend

Next.js (App Router) + TypeScript + Tailwind CSS + shadcn/ui + React Query.
See the repository root [`README.md`](../README.md) and [`docs/`](../docs)
for full project documentation.

## Layout

```
src/
├── app/              Routes, layouts, and the React Query provider
├── components/
│   └── ui/            shadcn/ui primitives (Button, Card, ...)
├── hooks/               React Query hooks
└── lib/                  Utilities and the API client
```

## Quickstart

```bash
npm install
npm run dev          # start dev server on :3000
npm run build         # production build
npm run lint            # eslint
npm run typecheck        # tsc --noEmit
npm run test               # vitest
npm run format               # prettier --write
```

Copy `.env.example` to `.env.local` and adjust `NEXT_PUBLIC_API_BASE_URL` if
the backend isn't running on the default port.

## Adding shadcn/ui components

This project is pre-configured for the shadcn/ui CLI (`components.json`).
Add new primitives with:

```bash
npx shadcn@latest add <component>
```

See [`../docs/CODING_STANDARDS.md`](../docs/CODING_STANDARDS.md) for
conventions.
