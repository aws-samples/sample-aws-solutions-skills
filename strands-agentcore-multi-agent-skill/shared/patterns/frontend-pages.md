# Frontend Pages

> A chat UI based on **React 18 + Vite + TypeScript + Tailwind v3 + shadcn/ui**. Cognito login via Amplify Authenticator, with progressive rendering of the AgentCore Runtime's SSE stream.

## File layout

```
frontend/
├── package.json
├── tsconfig.json
├── tsconfig.app.json
├── tsconfig.node.json
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── components.json                    ← shadcn config
├── eslint.config.js
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx                        ← Amplify init + Authenticator + router
    ├── index.css                      ← Tailwind + shadcn variables
    ├── pages/
    │   └── chat.tsx                   ← main chat page
    ├── components/
    │   ├── chat/
    │   │   └── chat-sidebar.tsx       ← session list, examples
    │   └── ui/                        ← shadcn primitives
    │       ├── card.tsx
    │       ├── button.tsx
    │       ├── input.tsx
    │       ├── textarea.tsx
    │       ├── badge.tsx
    │       ├── skeleton.tsx
    │       ├── tabs.tsx
    │       ├── select.tsx
    │       ├── dialog.tsx
    │       └── ...
    ├── store/
    │   └── app-store.ts               ← zustand global state (config, sessions, messages)
    ├── hooks/
    │   └── use-mobile.tsx
    ├── lib/
    │   └── utils.ts                   ← cn() helper
    └── api/
        └── orchestrator.ts            ← SSE POST + parser
└── public/
    ├── config.json                    ← injected at deploy time (Cognito + endpoint URL)
    ├── favicon.png
    └── splash.png
```

## `package.json`

```json
{
  "name": "multi-agent-chat",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "lint": "eslint ."
  },
  "dependencies": {
    "@aws-amplify/ui-react": "^6.11.0",
    "aws-amplify": "^6.14.2",
    "@radix-ui/react-avatar": "^1.1.4",
    "@radix-ui/react-dialog": "^1.1.7",
    "@radix-ui/react-dropdown-menu": "^2.1.7",
    "@radix-ui/react-label": "^2.1.3",
    "@radix-ui/react-select": "^2.1.7",
    "@radix-ui/react-separator": "^1.1.3",
    "@radix-ui/react-slot": "^1.2.0",
    "@radix-ui/react-switch": "^1.1.4",
    "@radix-ui/react-tabs": "^1.1.4",
    "@radix-ui/react-tooltip": "^1.2.0",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "lucide-react": "^0.475.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-markdown": "^10.1.0",
    "react-router": "^7.5.0",
    "remark-gfm": "^4.0.1",
    "tailwind-merge": "^3.0.1",
    "tailwindcss-animate": "^1.0.7",
    "zustand": "^5.0.3"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "@types/react": "^18.3.1",
    "@types/react-dom": "^18.3.1",
    "autoprefixer": "^10.4.20",
    "postcss": "^8.5.2",
    "tailwindcss": "3",
    "typescript": "~5.7.2",
    "vite": "^6.1.0"
  }
}
```

## `vite.config.ts`

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
});
```

## `tailwind.config.js`

```js
/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: { DEFAULT: "hsl(var(--primary))", foreground: "hsl(var(--primary-foreground))" },
        secondary: { DEFAULT: "hsl(var(--secondary))", foreground: "hsl(var(--secondary-foreground))" },
        muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
        accent: { DEFAULT: "hsl(var(--accent))", foreground: "hsl(var(--accent-foreground))" },
        destructive: { DEFAULT: "hsl(var(--destructive))", foreground: "hsl(var(--destructive-foreground))" },
        card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
      },
      borderRadius: { lg: "var(--radius)", md: "calc(var(--radius) - 2px)", sm: "calc(var(--radius) - 4px)" },
    },
  },
  plugins: [require("tailwindcss-animate"), require("@tailwindcss/typography")],
};
```

## `src/index.css`

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :root {
    --background: 0 0% 100%;
    --foreground: 222.2 84% 4.9%;
    --primary: 221.2 83.2% 53.3%;
    --primary-foreground: 210 40% 98%;
    --secondary: 210 40% 96.1%;
    --secondary-foreground: 222.2 47.4% 11.2%;
    --muted: 210 40% 96.1%;
    --muted-foreground: 215.4 16.3% 46.9%;
    --accent: 210 40% 96.1%;
    --accent-foreground: 222.2 47.4% 11.2%;
    --destructive: 0 84.2% 60.2%;
    --destructive-foreground: 210 40% 98%;
    --border: 214.3 31.8% 91.4%;
    --card: 0 0% 100%;
    --card-foreground: 222.2 84% 4.9%;
    --radius: 0.5rem;
  }
}
```

## `src/lib/utils.ts`

```ts
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

## `src/main.tsx`

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

## `src/App.tsx`

```tsx
import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router";
import { Amplify } from "aws-amplify";
import { Authenticator } from "@aws-amplify/ui-react";
import { ChatPage } from "./pages/chat";
import { useAppStore } from "./store/app-store";
import "@aws-amplify/ui-react/styles.css";

function AuthenticatedApp() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="/chat" element={<ChatPage />} />
      </Routes>
    </BrowserRouter>
  );
}

export default function App() {
  const [loaded, setLoaded] = useState(false);
  const setConfig = useAppStore((s) => s.setConfig);

  useEffect(() => {
    fetch("/config.json")
      .then((r) => r.json())
      .then((cfg) => {
        Amplify.configure({
          Auth: {
            Cognito: {
              userPoolId: cfg.cognito.userPoolId,
              userPoolClientId: cfg.cognito.clientId,
            },
          },
        });
        setConfig(cfg);
        setLoaded(true);
      });
  }, [setConfig]);

  if (!loaded) {
    return <div className="flex h-screen items-center justify-center">Loading...</div>;
  }

  return (
    <Authenticator
      loginMechanisms={["username"]}
      components={{
        Header() {
          return (
            <div className="text-center py-6">
              <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-gradient-to-br from-blue-500 to-indigo-600">
                <span className="text-3xl">🤖</span>
              </div>
              <h1 className="text-2xl font-bold">Multi-Agent Chat</h1>
            </div>
          );
        },
      }}
    >
      <AuthenticatedApp />
    </Authenticator>
  );
}
```

## `src/store/app-store.ts`

```ts
import { create } from "zustand";

interface Config {
  cognito: { userPoolId: string; clientId: string };
  endpoints: { orchestrator: string };
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
}

interface AppStore {
  config: Config | null;
  setConfig: (c: Config) => void;
  sessionId: string;                    // ★ UUID — never timestamp (constraint #25)
  setSessionId: (id: string) => void;
  messages: ChatMessage[];
  addMessage: (m: ChatMessage) => void;
  appendToLastAssistant: (delta: string) => void;
  resetSession: () => void;
}

export const useAppStore = create<AppStore>((set) => ({
  config: null,
  setConfig: (c) => set({ config: c }),
  sessionId: `session-${crypto.randomUUID()}`,                        // ★ UUID
  setSessionId: (id) => set({ sessionId: id }),
  messages: [],
  addMessage: (m) => set((s) => ({ messages: [...s.messages, m] })),
  appendToLastAssistant: (delta) =>
    set((s) => {
      const msgs = [...s.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === "assistant") last.content += delta;
      return { messages: msgs };
    }),
  resetSession: () => set({ sessionId: `session-${crypto.randomUUID()}`, messages: [] }), // ★ UUID
}));
```

## `src/api/orchestrator.ts`

```ts
import { fetchAuthSession } from "aws-amplify/auth";
import { useAppStore } from "../store/app-store";

export async function streamOrchestrator(prompt: string, onDelta: (text: string) => void): Promise<void> {
  const session = await fetchAuthSession();
  const accessToken = session.tokens?.accessToken?.toString();

  // ★ Constraint #25 — Memory `actor_id` MUST be the stable Cognito sub.
  // ID token's `sub` claim is the user pool's permanent identifier for this user.
  const userSub = session.tokens?.idToken?.payload.sub as string | undefined;

  if (!accessToken) throw new Error("No auth token");
  if (!userSub) throw new Error("No user sub from Cognito ID token — cannot maintain Memory continuity");

  const { config, sessionId } = useAppStore.getState();
  if (!config) throw new Error("Config not loaded");

  const res = await fetch(config.endpoints.orchestrator + "?qualifier=DEFAULT", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      prompt,
      customer_id: `cognito_${userSub}`,        // ★ Stable across sessions/devices
      session_id: sessionId,                    // ★ UUID from app-store
    }),
  });

  if (!res.ok || !res.body) {
    throw new Error(`Orchestrator returned ${res.status}: ${await res.text()}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // ── SSE: events split by "\n\n", each line starts with "data: "
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      processChunk(chunk, onDelta);
    }
  }
}

function processChunk(chunk: string, onDelta: (text: string) => void) {
  for (const line of chunk.split("\n")) {
    if (!line.startsWith("data: ")) continue;
    try {
      const data = JSON.parse(line.slice(6));
      // Strands stream event shape:
      // { event: { contentBlockDelta: { delta: { text: "..." } } } }
      const text = data?.event?.contentBlockDelta?.delta?.text;
      if (text) onDelta(text);
    } catch {
      // Ignore malformed lines
    }
  }
}
```

## `src/pages/chat.tsx`

```tsx
import { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Send, Plus } from "lucide-react";
import { useAuthenticator } from "@aws-amplify/ui-react";
import { useAppStore } from "../store/app-store";
import { streamOrchestrator } from "../api/orchestrator";
import { Card } from "../components/ui/card";
import { Button } from "../components/ui/button";
import { Textarea } from "../components/ui/textarea";

const EXAMPLES = [
  { label: "📋 Open issues in DEMO project", prompt: "Show me all open issues in project DEMO" },
  { label: "💻 Recent commits in our repo", prompt: "Show recent commits in owner/repo" },
  { label: "📊 Top customers by sales", prompt: "Show me total sales by customer" },
  { label: "📚 What is AgentCore?", prompt: "What is Amazon Bedrock AgentCore?" },
];

export function ChatPage() {
  const { signOut } = useAuthenticator();
  const { messages, addMessage, appendToLastAssistant, resetSession } = useAppStore();
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(text: string) {
    if (!text.trim() || busy) return;
    setBusy(true);
    addMessage({ id: `${Date.now()}-u`, role: "user", content: text, timestamp: Date.now() });
    addMessage({ id: `${Date.now()}-a`, role: "assistant", content: "", timestamp: Date.now() });

    try {
      await streamOrchestrator(text, (delta) => appendToLastAssistant(delta));
    } catch (e: any) {
      appendToLastAssistant(`\n\n_Error: ${e.message}_`);
    } finally {
      setBusy(false);
      setInput("");
    }
  }

  return (
    <div className="flex h-screen flex-col bg-slate-50">
      <header className="flex items-center justify-between border-b bg-white px-6 py-4">
        <h1 className="text-xl font-bold">Multi-Agent Chat</h1>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={resetSession}>
            <Plus className="mr-2 h-4 w-4" /> New session
          </Button>
          <Button variant="ghost" size="sm" onClick={signOut}>Sign out</Button>
        </div>
      </header>

      <main className="flex flex-1 flex-col overflow-hidden">
        {messages.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-6 px-4">
            <p className="text-center text-slate-600">Ask anything or pick an example below</p>
            <div className="grid grid-cols-2 gap-3 max-w-2xl">
              {EXAMPLES.map((ex) => (
                <Card
                  key={ex.label}
                  className="cursor-pointer p-4 hover:shadow-md transition-shadow"
                  onClick={() => send(ex.prompt)}
                >
                  <p className="text-sm font-medium">{ex.label}</p>
                </Card>
              ))}
            </div>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto px-4 py-6">
            <div className="mx-auto max-w-3xl space-y-4">
              {messages.map((m) => (
                <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <Card className={`max-w-[85%] p-4 ${m.role === "user" ? "bg-blue-600 text-white" : "bg-white"}`}>
                    {m.role === "user" ? (
                      <p>{m.content}</p>
                    ) : (
                      <ReactMarkdown remarkPlugins={[remarkGfm]} className="prose prose-sm max-w-none">
                        {m.content || "▍"}
                      </ReactMarkdown>
                    )}
                  </Card>
                </div>
              ))}
              <div ref={bottomRef} />
            </div>
          </div>
        )}
      </main>

      <footer className="border-t bg-white p-4">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            placeholder="Type a message..."
            className="min-h-[60px] max-h-32 resize-none"
            disabled={busy}
          />
          <Button onClick={() => send(input)} disabled={busy || !input.trim()}>
            <Send className="h-4 w-4" />
          </Button>
        </div>
      </footer>
    </div>
  );
}
```

## shadcn primitives — `components/ui/card.tsx` (example)

```tsx
import * as React from "react";
import { cn } from "@/lib/utils";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("rounded-lg border bg-card text-card-foreground shadow-sm", className)} {...props} />
  ),
);
Card.displayName = "Card";

export const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex flex-col space-y-1.5 p-6", className)} {...props} />
  ),
);
CardHeader.displayName = "CardHeader";

export const CardTitle = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <h3 ref={ref as any} className={cn("text-lg font-semibold leading-none tracking-tight", className)} {...props} />
  ),
);
CardTitle.displayName = "CardTitle";

export const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("p-6 pt-0", className)} {...props} />
  ),
);
CardContent.displayName = "CardContent";
```

> The other shadcn primitives (`button`, `input`, `textarea`, `badge`, `tabs`, `dialog`, `select`, `skeleton`) are added in bulk with `npx shadcn@latest add <component>`. Set the following in components.json:

```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "default",
  "tailwind": {
    "config": "tailwind.config.js",
    "css": "src/index.css",
    "baseColor": "slate",
    "cssVariables": true
  },
  "rsc": false,
  "tsx": true,
  "aliases": {
    "components": "@/components",
    "utils": "@/lib/utils",
    "ui": "@/components/ui"
  }
}
```

## `public/config.json` (injected at deploy time)

```json
{
  "cognito": {
    "userPoolId": "us-east-1_XXXXXXXXX",
    "clientId": "XXXXXXXXXXXXXXXXXXXXXXXXXX"
  },
  "endpoints": {
    "orchestrator": "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<URL-encoded-arn>/invocations"
  }
}
```

## Deployment Pipeline

### Option A — Amplify Hosting (recommended)

```bash
cd frontend
pnpm install
pnpm build

# Deploy dist/ from the Amplify CLI or Console
```

Do not add an Auth category such as Cognito separately — inject the User Pool ID from the CDK deployment directly into `config.json`.

### Option B — S3 + CloudFront

```bash
aws s3 sync dist/ s3://my-frontend-bucket/ --delete
aws cloudfront create-invalidation --distribution-id ABCDEF --paths "/*"
```

### Auto-generate config.json (`scripts/generate-frontend-config.sh`)

```bash
#!/usr/bin/env bash
set -e

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name MultiAgentOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)
CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name MultiAgentOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`ClientId`].OutputValue' \
  --output text)
RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --stack-name MultiAgentOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`RuntimeArn`].OutputValue' \
  --output text)
REGION=$(aws configure get region)

ENCODED_ARN=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$RUNTIME_ARN', safe=''))")

cat > frontend/public/config.json <<EOF
{
  "cognito": {
    "userPoolId": "$USER_POOL_ID",
    "clientId": "$CLIENT_ID"
  },
  "endpoints": {
    "orchestrator": "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations"
  }
}
EOF
echo "✓ Wrote frontend/public/config.json"
```

## Pitfalls

| Pitfall | Avoidance |
|---|---|
| Importing `config.json` via `import` | Use `fetch("/config.json")` — allows per-environment replacement after build |
| Missing ARN encoding in the `endpoints.orchestrator` URL | Use `urllib.parse.quote(arn, safe="")` as in `generate-frontend-config.sh` above |
| Amplify v6 import path | `aws-amplify/auth` (NOT `aws-amplify`) — fetchAuthSession, etc. |
| SSE chunks not split on `\n\n` boundaries | Handle partial chunks across various reader implementations — use this code's buffer pattern |
| Using Cloudscape | ❌ Cloudscape NOT used — this skill uses shadcn/ui only |
| Auto-refresh on 401 | Recommend adding the Amplify v6 `fetchAuthSession({ forceRefresh: true })` call pattern |
| Missing `customer_id` or using unofficial fields like `session.userSub` | **Always extract `idToken.payload.sub` → explicitly send as `cognito_${sub}`** (constraint #25). If missing, the backend creates a new actor on every call → Memory is nullified |
| `sessionId` based on `Date.now()` | **UUID (`crypto.randomUUID()`) is required** — a timestamp collision could cause a cross-user data leak |
