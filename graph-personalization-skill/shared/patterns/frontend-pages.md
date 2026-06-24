# Frontend Pages — Admin/Demo UI

> React 18 + Vite + TypeScript + Tailwind v3 + shadcn/ui + Cognito Authenticator. **2 main pages**: Graph Explorer (user-centric graph visualization) + Recommendation Demo (recommendation results + Bedrock explanation).

## File layout

```
frontend/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── tailwind.config.js
├── components.json
└── src/
    ├── main.tsx
    ├── App.tsx                          (Amplify Authenticator + router)
    ├── index.css                         (Tailwind + shadcn)
    ├── pages/
    │   ├── GraphExplorer.tsx            (vis-network user-centric graph)
    │   ├── RecommendationDemo.tsx       (recommendation + explanation + score breakdown)
    │   └── AdminDashboard.tsx           (graph stats, schema, ingest log)
    ├── components/
    │   ├── GraphView.tsx                (vis-network wrapper)
    │   ├── RecommendationCard.tsx
    │   ├── ExplanationBox.tsx
    │   ├── ScenarioSelector.tsx
    │   └── ui/                          (shadcn primitives)
    ├── api/client.ts                     (typed API client + Cognito JWT)
    ├── store/auth.ts                     (Zustand)
    ├── store/config.ts                   (config.json fetched at runtime)
    ├── lib/utils.ts                      (cn() helper)
    └── types/graph.ts                    (TypeScript types for graph data)
└── public/config.json                    (generated from SSM after deploy)
```

## `package.json`

```json
{
  "name": "graph-personalization-frontend",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router": "^7.5.0",
    "@aws-amplify/ui-react": "^6.11.0",
    "aws-amplify": "^6.14.2",
    "vis-network": "^9.1.10",
    "vis-data": "^7.1.10",
    "zustand": "^5.0.3",
    "lucide-react": "^0.475.0",
    "react-markdown": "^10.1.0",
    "@radix-ui/react-tabs": "^1.1.4",
    "@radix-ui/react-dialog": "^1.1.7",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "tailwind-merge": "^3.0.1",
    "tailwindcss-animate": "^1.0.7"
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

## `src/App.tsx`

```tsx
import { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate, Link } from 'react-router';
import { Amplify } from 'aws-amplify';
import { Authenticator } from '@aws-amplify/ui-react';
import GraphExplorer from './pages/GraphExplorer';
import RecommendationDemo from './pages/RecommendationDemo';
import AdminDashboard from './pages/AdminDashboard';
import { useConfig } from './store/config';
import '@aws-amplify/ui-react/styles.css';

function AuthenticatedApp() {
  return (
    <BrowserRouter>
      <nav className="border-b bg-white px-6 py-4 flex gap-4">
        <Link to="/demo" className="font-semibold">Recommendation Demo</Link>
        <Link to="/explorer">Graph Explorer</Link>
        <Link to="/admin">Admin</Link>
      </nav>
      <Routes>
        <Route path="/" element={<Navigate to="/demo" replace />} />
        <Route path="/demo" element={<RecommendationDemo />} />
        <Route path="/explorer" element={<GraphExplorer />} />
        <Route path="/admin" element={<AdminDashboard />} />
      </Routes>
    </BrowserRouter>
  );
}

export default function App() {
  const [loaded, setLoaded] = useState(false);
  const setConfig = useConfig((s) => s.setConfig);

  useEffect(() => {
    fetch('/config.json')
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

  if (!loaded) return <div className="p-8">Loading...</div>;

  return (
    <Authenticator loginMechanisms={['username', 'email']}>
      <AuthenticatedApp />
    </Authenticator>
  );
}
```

## `src/api/client.ts`

```tsx
import { fetchAuthSession } from 'aws-amplify/auth';
import { useConfig } from '../store/config';

async function authedFetch(path: string, opts: RequestInit = {}) {
  const session = await fetchAuthSession();
  const token = session.tokens?.idToken?.toString();
  const config = useConfig.getState().config;
  if (!config) throw new Error('Config not loaded');

  const res = await fetch(`${config.apiUrl}${path}`, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': token!,
      ...(opts.headers ?? {}),
    },
  });
  if (!res.ok) throw new Error(`API ${path} ${res.status}: ${await res.text()}`);
  return res.json();
}

// Typed endpoints
export interface RecommendationResult {
  items: { id: string; name: string; score: number }[];
  explanation: { explanation: string; reason_tag: string };
  scenario: string;
  count: number;
}

export const fetchRecommendations = (userId: string, scenario: 'collaborative' | 'cross-sell' | 'popular', limit = 10) =>
  authedFetch(`recommendations/${scenario}`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId, limit }),
  }) as Promise<RecommendationResult>;

export interface GraphNeighborhood {
  user: { id: string; segment?: string };
  neighborhood: { item: { id: string; name: string }; edge: { type: string; at: number; weight: number } }[];
}

export const fetchUserNeighborhood = (userId: string, limit = 20) =>
  authedFetch(`graph/explore?user_id=${userId}&limit=${limit}`) as Promise<GraphNeighborhood>;

export interface SimilarUsersGraph {
  center: { id: string; segment?: string };
  similars: { user: { id: string }; sharedCount: number; sharedItems: { id: string; name: string }[] }[];
}

export const fetchSimilarUsers = (userId: string, limit = 10) =>
  authedFetch(`graph/similar-users?user_id=${userId}&limit=${limit}`) as Promise<SimilarUsersGraph>;

export interface AdminStats {
  userCount: number;
  itemCount: number;
  edges: { type: string; count: number }[];
  coldStartUserCount: number;
}

export const fetchAdminStats = () => authedFetch('admin/stats') as Promise<AdminStats>;
```

## `src/pages/RecommendationDemo.tsx`

```tsx
import { useState } from 'react';
import { fetchRecommendations, type RecommendationResult } from '../api/client';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Card } from '../components/ui/card';
import { Badge } from '../components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../components/ui/tabs';

const SCENARIOS = [
  { id: 'collaborative', label: 'Similar Users', desc: 'Similar purchase patterns' },
  { id: 'cross-sell', label: 'Cross-sell', desc: 'Frequently bought together' },
  { id: 'popular', label: 'Popular (Cold start)', desc: 'Popular items in segment' },
] as const;

export default function RecommendationDemo() {
  const [userId, setUserId] = useState('u-1');
  const [scenario, setScenario] = useState<typeof SCENARIOS[number]['id']>('collaborative');
  const [result, setResult] = useState<RecommendationResult | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleFetch() {
    setLoading(true);
    setResult(null);
    try {
      const data = await fetchRecommendations(userId, scenario);
      setResult(data);
    } catch (e: any) {
      alert(`Error: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="container mx-auto p-6 space-y-6 max-w-4xl">
      <h1 className="text-2xl font-bold">Recommendation Demo</h1>

      <Card className="p-6 space-y-4">
        <div className="flex gap-3 items-end">
          <div className="flex-1">
            <label className="block text-sm font-medium mb-1">User ID</label>
            <Input value={userId} onChange={(e) => setUserId(e.target.value)} placeholder="u-1" />
          </div>
          <Button onClick={handleFetch} disabled={loading}>
            {loading ? 'Loading...' : 'Get Recommendations'}
          </Button>
        </div>

        <Tabs value={scenario} onValueChange={(v) => setScenario(v as any)}>
          <TabsList>
            {SCENARIOS.map((s) => (
              <TabsTrigger key={s.id} value={s.id}>{s.label}</TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </Card>

      {result && (
        <>
          {/* Bedrock explanation box */}
          <Card className="p-6 bg-blue-50 border-blue-200">
            <div className="flex items-start gap-3">
              <Badge variant="secondary">{result.explanation.reason_tag}</Badge>
              <p className="text-sm leading-relaxed">{result.explanation.explanation}</p>
            </div>
          </Card>

          {/* Recommendation results */}
          <div className="grid grid-cols-2 gap-4">
            {result.items.map((item, idx) => (
              <Card key={item.id} className="p-4">
                <div className="flex justify-between items-start mb-2">
                  <span className="font-semibold">{item.name}</span>
                  <Badge>#{idx + 1}</Badge>
                </div>
                <div className="text-sm text-gray-500">
                  ID: {item.id} · Score: {item.score.toFixed(2)}
                </div>
                <div className="mt-2">
                  <ScoreBar score={item.score} max={result.items[0]?.score ?? 1} />
                </div>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ScoreBar({ score, max }: { score: number; max: number }) {
  const pct = Math.min(100, (score / max) * 100);
  return (
    <div className="w-full h-2 bg-gray-200 rounded">
      <div className="h-full bg-blue-500 rounded" style={{ width: `${pct}%` }} />
    </div>
  );
}
```

## `src/pages/GraphExplorer.tsx` (vis-network)

```tsx
import { useEffect, useRef, useState } from 'react';
import { Network } from 'vis-network';
import { DataSet } from 'vis-data';
import { fetchUserNeighborhood, fetchSimilarUsers } from '../api/client';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Card } from '../components/ui/card';
import { Tabs, TabsList, TabsTrigger } from '../components/ui/tabs';

type ViewMode = 'neighborhood' | 'similar-users';

export default function GraphExplorer() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const networkRef = useRef<Network | null>(null);
  const [userId, setUserId] = useState('u-1');
  const [mode, setMode] = useState<ViewMode>('neighborhood');
  const [loading, setLoading] = useState(false);

  async function handleFetch() {
    if (!containerRef.current) return;
    setLoading(true);

    const nodes = new DataSet<any>([]);
    const edges = new DataSet<any>([]);

    try {
      if (mode === 'neighborhood') {
        const data = await fetchUserNeighborhood(userId, 20);
        // Center user
        nodes.add({ id: data.user.id, label: data.user.id, group: 'user', shape: 'circle' });
        // Items
        for (const { item, edge } of data.neighborhood) {
          nodes.add({ id: item.id, label: item.name, group: 'item', shape: 'box' });
          edges.add({
            from: data.user.id,
            to: item.id,
            label: edge.type,
            arrows: 'to',
            width: Math.min(edge.weight, 5),
          });
        }
      } else {
        const data = await fetchSimilarUsers(userId, 10);
        nodes.add({ id: data.center.id, label: data.center.id, group: 'center-user', shape: 'circle' });
        for (const sim of data.similars) {
          nodes.add({ id: sim.user.id, label: `${sim.user.id} (${sim.sharedCount} shared)`, group: 'similar-user' });
          edges.add({ from: data.center.id, to: sim.user.id, label: `${sim.sharedCount} shared`, dashes: true });
          // Shared items
          for (const item of sim.sharedItems.slice(0, 3)) {
            nodes.add({ id: item.id, label: item.name, group: 'item', shape: 'box' });
            edges.add({ from: data.center.id, to: item.id, color: { color: '#10b981' } });
            edges.add({ from: sim.user.id, to: item.id, color: { color: '#10b981' } });
          }
        }
      }

      // Render
      if (networkRef.current) networkRef.current.destroy();
      networkRef.current = new Network(
        containerRef.current,
        { nodes, edges },
        {
          nodes: { font: { size: 14 } },
          edges: { font: { size: 11, align: 'middle' }, smooth: { enabled: true, type: 'continuous', roundness: 0.5 } },
          groups: {
            user: { color: { background: '#3b82f6', border: '#1e40af' }, font: { color: 'white' } },
            'center-user': { color: { background: '#dc2626', border: '#7f1d1d' }, font: { color: 'white' } },
            'similar-user': { color: { background: '#fbbf24', border: '#92400e' } },
            item: { color: { background: '#10b981', border: '#065f46' }, font: { color: 'white' } },
          },
          physics: { stabilization: { iterations: 100 } },
        },
      );
    } catch (e: any) {
      alert(`Error: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    handleFetch();
  }, []);   // initial load

  return (
    <div className="container mx-auto p-6 space-y-4 max-w-6xl">
      <h1 className="text-2xl font-bold">Graph Explorer</h1>

      <Card className="p-4">
        <div className="flex gap-3 items-end mb-3">
          <div className="flex-1">
            <label className="block text-sm font-medium mb-1">User ID</label>
            <Input value={userId} onChange={(e) => setUserId(e.target.value)} placeholder="u-1" />
          </div>
          <Tabs value={mode} onValueChange={(v) => setMode(v as ViewMode)}>
            <TabsList>
              <TabsTrigger value="neighborhood">User Neighborhood</TabsTrigger>
              <TabsTrigger value="similar-users">Similar Users</TabsTrigger>
            </TabsList>
          </Tabs>
          <Button onClick={handleFetch} disabled={loading}>
            {loading ? 'Loading...' : 'Explore'}
          </Button>
        </div>
        <div ref={containerRef} className="w-full h-[600px] border rounded bg-white" />
      </Card>

      <Card className="p-4 text-sm space-y-2">
        <div><strong>Legend</strong></div>
        <div className="flex gap-4">
          <span className="flex items-center gap-1"><span className="w-3 h-3 bg-blue-500 rounded-full inline-block" /> User</span>
          <span className="flex items-center gap-1"><span className="w-3 h-3 bg-green-500 inline-block" /> Item</span>
          <span className="flex items-center gap-1"><span className="w-3 h-3 bg-yellow-400 rounded-full inline-block" /> Similar User</span>
        </div>
      </Card>
    </div>
  );
}
```

## `src/pages/AdminDashboard.tsx`

```tsx
import { useEffect, useState } from 'react';
import { fetchAdminStats, type AdminStats } from '../api/client';
import { Card } from '../components/ui/card';

export default function AdminDashboard() {
  const [stats, setStats] = useState<AdminStats | null>(null);

  useEffect(() => {
    fetchAdminStats().then(setStats).catch((e) => alert(e.message));
  }, []);

  if (!stats) return <div className="p-8">Loading...</div>;

  return (
    <div className="container mx-auto p-6 space-y-6 max-w-4xl">
      <h1 className="text-2xl font-bold">Admin Dashboard</h1>

      <div className="grid grid-cols-3 gap-4">
        <StatCard label="Users" value={stats.userCount} />
        <StatCard label="Items" value={stats.itemCount} />
        <StatCard label="Cold-start Users" value={stats.coldStartUserCount} highlight={stats.coldStartUserCount > 100} />
      </div>

      <Card className="p-6">
        <h2 className="text-lg font-semibold mb-3">Edges by type</h2>
        <table className="w-full text-sm">
          <thead className="border-b">
            <tr>
              <th className="text-left py-2">Edge Type</th>
              <th className="text-right py-2">Count</th>
            </tr>
          </thead>
          <tbody>
            {stats.edges.map((e) => (
              <tr key={e.type} className="border-b">
                <td className="py-2">{e.type}</td>
                <td className="text-right">{e.count.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function StatCard({ label, value, highlight }: { label: string; value: number; highlight?: boolean }) {
  return (
    <Card className={`p-6 ${highlight ? 'bg-yellow-50 border-yellow-300' : ''}`}>
      <div className="text-sm text-gray-500">{label}</div>
      <div className="text-2xl font-bold mt-1">{value.toLocaleString()}</div>
    </Card>
  );
}
```

## Build + Deploy

```bash
# Build
pnpm install
pnpm build
# → index.html + assets/ inside dist/

# Generate config.json (after deploy)
aws ssm get-parameter \
  --name "/graph-personalization/prod/frontend-config" \
  --query 'Parameter.Value' --output text > public/config.json

# Sync to S3
aws s3 sync dist/ s3://graph-personalization-prod-frontend-${ACCOUNT}/ --delete
aws cloudfront create-invalidation \
  --distribution-id ${DIST_ID} \
  --paths "/index.html"
```

## Local dev

```bash
# The backend (Lambda) sits behind ALB / API Gateway, so direct local calls are difficult.
# Option 1: deploy a dev environment and use that API (simple)
# Option 2: Lambda emulation with SAM Local or awslocal
# Option 3: Mock API server (msw) — mock graph data for the demo

pnpm dev   # http://localhost:5173
```

## Pitfall avoidance (see constraints #11, #22)

| Pitfall | Handling |
|---|---|
| #11 vis-network performance drops on large graphs | top 20 nodes + click-to-expand |
| #22 build-time env var | load at runtime via `fetch('/config.json')` |
| Cognito callback URL mismatch | exactly match the Cognito client config and the frontend URL |
| Network color blindness | also use shapes (circle/box) |
| Vis-network mobile performance | reduce layout options on small viewports |
| API returns 401 | full reload → ALB / API GW Cognito redirect |
