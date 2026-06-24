# Frontend Page Patterns

**Stack**: React 18 + Vite + TypeScript + Tailwind v3 + shadcn/ui + Radix primitives + lucide-react + recharts + sonner. **Cloudscape is not used.**

## ⚠️ Required: backend API connection (if skipped, the UI shows a blank screen)

The frontend must connect to the deployed API Gateway endpoint. **If this connection is missing, the pages render but no data appears.**

### 1. Outputs that CDK must expose

```typescript
// Always add at the bottom of lib/api-stack.ts (or main-stack.ts)
new cdk.CfnOutput(this, 'ApiUrl', { value: api.url });
new cdk.CfnOutput(this, 'CognitoUserPoolId', { value: userPool.userPoolId });
new cdk.CfnOutput(this, 'CognitoClientId', { value: userPoolClient.userPoolClientId });
new cdk.CfnOutput(this, 'CognitoDomain', { value: `${projectName}-${cdk.Aws.ACCOUNT_ID}.auth.${cdk.Aws.REGION}.amazoncognito.com` });
new cdk.CfnOutput(this, 'Region', { value: cdk.Aws.REGION });
```

### 2. Automatic environment variable setup (`scripts/update-frontend-env.sh`)

```bash
#!/bin/bash
STACK="${1:-${PROJECT_NAME:-app}-stack}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
get() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

cat > frontend/.env.local <<EOF
VITE_API_URL=$(get ApiUrl)
VITE_COGNITO_USER_POOL_ID=$(get CognitoUserPoolId)
VITE_COGNITO_CLIENT_ID=$(get CognitoClientId)
VITE_COGNITO_DOMAIN=$(get CognitoDomain)
VITE_REGION=$REGION
EOF
echo "✅ frontend/.env.local updated"
```

Call this script at the end of `scripts/deploy.sh` to automate it.

## Project structure

```
frontend/
├── index.html
├── vite.config.ts
├── tailwind.config.ts
├── postcss.config.js
├── components.json           ← shadcn registry
├── package.json
├── src/
│   ├── main.tsx
│   ├── App.tsx               ← Router + AuthProvider
│   ├── index.css             ← Tailwind directives + CSS vars
│   ├── lib/utils.ts          ← `cn()` (twMerge + clsx)
│   ├── api/client.ts         ← apiCall + auto-refresh
│   ├── api/auth.ts           ← OIDC userManager
│   ├── pages/                ← route components
│   ├── components/
│   │   ├── ui/               ← shadcn components (button, card, badge, alert, table, select, tabs, dialog, skeleton, ...)
│   │   ├── Layout.tsx
│   │   ├── PageHeader.tsx
│   │   └── StatCard.tsx
│   └── hooks/
```

## package.json key dependencies

```json
{
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "react-router-dom": "^6.20.0",
    "@radix-ui/react-dialog": "^1.0.5",
    "@radix-ui/react-dropdown-menu": "^2.0.6",
    "@radix-ui/react-label": "^2.0.2",
    "@radix-ui/react-select": "^2.0.0",
    "@radix-ui/react-slot": "^1.0.2",
    "@radix-ui/react-tabs": "^1.0.4",
    "@radix-ui/react-tooltip": "^1.0.7",
    "class-variance-authority": "^0.7.0",
    "clsx": "^2.1.0",
    "tailwind-merge": "^2.2.0",
    "tailwindcss-animate": "^1.0.7",
    "lucide-react": "^0.469.0",
    "recharts": "^2.10.4",
    "sonner": "^1.4.0",
    "oidc-client-ts": "^3.0.1",
    "react-oidc-context": "^3.1.1"
  },
  "devDependencies": {
    "tailwindcss": "^3.4.0",
    "autoprefixer": "^10.4.16",
    "postcss": "^8.4.32",
    "@vitejs/plugin-react": "^4.2.1",
    "vite": "^5.0.0",
    "typescript": "^5.3.0"
  }
}
```

## tailwind.config.ts

```typescript
import type { Config } from 'tailwindcss';
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Use the standard shadcn CSS variables as-is
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: { DEFAULT: 'hsl(var(--primary))', foreground: 'hsl(var(--primary-foreground))' },
        muted: { DEFAULT: 'hsl(var(--muted))', foreground: 'hsl(var(--muted-foreground))' },
        // ... destructive, accent, card, popover, border, input, ring
      },
    },
  },
  plugins: [require('tailwindcss-animate')],
} satisfies Config;
```

## App.tsx — Router + Layout (shadcn)

```tsx
import { BrowserRouter, Routes, Route, Navigate, Link, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from 'react-oidc-context';
import { Toaster } from 'sonner';
import { LayoutDashboard, Database, Network, GaugeCircle, Sparkles, Users2, GitGraph, Send } from 'lucide-react';
import Layout from './components/Layout';
import DashboardPage from './pages/DashboardPage';
// ... other pages

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/ingestion', label: 'Data Ingestion', icon: Database },
  { to: '/matching', label: 'Entity Matching', icon: Network },
  { to: '/accuracy', label: 'Accuracy', icon: GaugeCircle },
  { to: '/ai-rules', label: 'AI Rules', icon: Sparkles },
  { to: '/profile-import', label: 'Send to CP', icon: Send },     // note 5: Send-to-CP screen
  { to: '/profiles', label: 'Unified Profile', icon: Users2 },
  { to: '/graph', label: 'Knowledge Graph', icon: GitGraph },     // optional
];

export default function App() {
  return (
    <AuthProvider {...oidcConfig}>
      <BrowserRouter>
        <Layout nav={NAV}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            {/* ... */}
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </Layout>
        <Toaster position="top-right" richColors />
      </BrowserRouter>
    </AuthProvider>
  );
}
```

## Layout.tsx — sidebar + topbar (shadcn pattern)

```tsx
import { Link, useLocation } from 'react-router-dom';
import { cn } from '../lib/utils';

export default function Layout({ nav, children }: { nav: NavItem[]; children: React.ReactNode }) {
  const { pathname } = useLocation();
  return (
    <div className="min-h-screen bg-background text-foreground">
      <aside className="fixed inset-y-0 left-0 w-60 border-r bg-card">
        <div className="px-6 py-5 border-b">
          <h1 className="font-semibold tracking-tight">Unified Customer Profile</h1>
        </div>
        <nav className="p-3 space-y-1">
          {nav.map(({ to, label, icon: Icon }) => (
            <Link key={to} to={to}
              className={cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors',
                pathname === to ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              )}>
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="ml-60 p-6">{children}</main>
    </div>
  );
}
```

## API Client — auto-attach token + 401 retry

```typescript
// src/api/client.ts
import { userManager } from './auth';
const API_URL = import.meta.env.VITE_API_URL;

async function token(): Promise<string> {
  const u = await userManager.getUser();
  if (!u || u.expired) {
    const refreshed = await userManager.signinSilent().catch(() => null);
    return refreshed?.id_token ?? '';
  }
  return u.id_token ?? '';
}

export async function apiCall<T>(path: string, init: RequestInit = {}): Promise<T> {
  const doFetch = async (t: string) =>
    fetch(`${API_URL}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${t}`, ...(init.headers ?? {}) },
    });
  let res = await doFetch(await token());
  if (res.status === 401) {
    // force one refresh + retry
    const refreshed = await userManager.signinSilent().catch(() => null);
    res = await doFetch(refreshed?.id_token ?? '');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `API ${res.status}`);
  }
  return res.json();
}
```

## Page pattern — use only standard shadcn components

Every page follows the same visual skeleton:

```tsx
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '../components/ui/card';
import { Button } from '../components/ui/button';
import { Badge } from '../components/ui/badge';
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert';
import { Skeleton } from '../components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../components/ui/tabs';
import PageHeader from '../components/PageHeader';
```

## DashboardPage — StatCard + Recharts

```tsx
export default function DashboardPage() {
  const [stats, setStats] = useState<any>(null);
  useEffect(() => { apiCall('/api/dashboard-summary').then(setStats); }, []);

  return (
    <>
      <PageHeader title="Dashboard" description="Unified customer profile overview" />
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Total Profiles" value={stats?.totalProfiles} icon={Users2} />
        <StatCard label="Matched Groups" value={stats?.matchedGroups} icon={Network} />
        <StatCard label="Match Rate" value={`${stats?.matchRate ?? 0}%`} icon={GaugeCircle} />
        <StatCard label="Data Sources" value={stats?.dataSources} icon={Database} />
      </div>
      <Card className="mt-6">
        <CardHeader><CardTitle>Daily Trend</CardTitle></CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={stats?.dailyTrend ?? []}>
              <XAxis dataKey="date" /><YAxis /><Tooltip />
              <Line dataKey="profiles" stroke="hsl(var(--primary))" />
            </LineChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </>
  );
}
```

## IngestionPage — data ingestion (3 modes)

```tsx
export default function IngestionPage() {
  const [mode, setMode] = useState<'csv'|'glue_connection'|'kinesis'>('glue_connection');
  const [running, setRunning] = useState(false);

  return (
    <>
      <PageHeader title="Data Ingestion" />
      <Tabs value={mode} onValueChange={v => setMode(v as any)}>
        <TabsList>
          <TabsTrigger value="csv">CSV Upload</TabsTrigger>
          <TabsTrigger value="glue_connection">Glue Connection (DB)</TabsTrigger>
          <TabsTrigger value="kinesis">Kinesis Stream</TabsTrigger>
        </TabsList>
        <TabsContent value="csv">
          {/* file picker → POST /api/ingestion/upload-csv */}
        </TabsContent>
        <TabsContent value="glue_connection">
          <Button onClick={async () => {
            setRunning(true);
            await apiCall('/api/ingestion/crawl', { method: 'POST' });
            toast.success('Crawler started');
            setRunning(false);
          }} disabled={running}>Start Crawler</Button>

          {/* ★ ETL pipeline trigger: raw → ER input */}
          <Button variant="secondary" onClick={async () => {
            await apiCall('/api/ingestion/build-er-input', { method: 'POST' });
            toast.success('ETL job started — converting raw → ER input');
          }}>Run ETL (Raw → ER Input)</Button>
        </TabsContent>
      </Tabs>
    </>
  );
}
```

## MatchingComparisonPage — run all 3 simultaneously + compare

```tsx
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend } from 'recharts';

export default function MatchingComparisonPage() {
  const [results, setResults] = useState<any[]>([]);
  const [running, setRunning] = useState(false);

  async function runAll() {
    setRunning(true);
    try {
      const [s, a, m] = await Promise.all([
        apiCall('/api/matching/run', { method: 'POST', body: JSON.stringify({ matchingType: 'simple' }) }),
        apiCall('/api/matching/run', { method: 'POST', body: JSON.stringify({ matchingType: 'advanced' }) }),
        apiCall('/api/matching/run', { method: 'POST', body: JSON.stringify({ matchingType: 'ml' }) }),
      ]);
      setResults([s, a, m]);
      toast.success('All 3 matching types complete');
    } finally { setRunning(false); }
  }

  return (
    <>
      <PageHeader title="Matching Comparison"
        actions={<Button onClick={runAll} disabled={running}>Run all 3 matching types</Button>} />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {results.map(r => (
          <Card key={r.type}>
            <CardHeader>
              <Badge variant={r.type === 'ml' ? 'destructive' : 'secondary'}>{r.label}</Badge>
              <CardTitle>{r.matchedGroups} groups</CardTitle>
            </CardHeader>
            <CardContent className="text-sm space-y-1">
              <p>Unmatched: <b>{r.unmatchedProfiles}</b></p>
              <p>Precision: <b>{(r.precision*100).toFixed(1)}%</b></p>
              <p>Recall: <b>{(r.recall*100).toFixed(1)}%</b></p>
              <p className="text-muted-foreground">{r.executionTime} · {r.cost}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  );
}
```

## ProfileImportPage — **the screen that sends to CP** (required)

A two-step screen that sends the matching results (golden records) to Customer Profiles. **This page must be included** — otherwise the ER results never reach CP.

### Steps
1. **Step 1: Preview & Import GuestProfile** — select the active matching type (simple/advanced/ml), preview, then `POST /api/profile-import/run`
2. **Step 2: Import Reservation/Folio** — after Step 1 finishes, `POST /api/cp-data-import/run` (PostgreSQL → CP child instances)

```tsx
export default function ProfileImportPage() {
  const [matchingType, setMatchingType] = useState<'simple'|'advanced'|'ml'>('ml');
  const [preview, setPreview] = useState<any>(null);
  const [importResult, setImportResult] = useState<any>(null);
  const [cpDataStatus, setCpDataStatus] = useState<any>(null);

  return (
    <>
      <PageHeader title="Send to Customer Profiles"
        description="ER matching results → CP GuestProfile, and PostgreSQL → CP Reservation/Folio" />

      {/* STEP 1 */}
      <Card>
        <CardHeader>
          <CardTitle>Step 1 · Golden Profile Import</CardTitle>
          <CardDescription>Merge matching results and send them as GuestProfiles to the CP domain</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Select value={matchingType} onValueChange={v => setMatchingType(v as any)}>
            <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="simple">Simple Rule</SelectItem>
              <SelectItem value="advanced">Advanced Rule</SelectItem>
              <SelectItem value="ml">ML Matching</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={async () => setPreview(
              await apiCall('/api/profile-import/preview', { method:'POST', body: JSON.stringify({ matchingType, limit: 8 }) })
            )}>Preview</Button>
            <Button onClick={async () => {
              if (!confirm(`This will send the ${matchingType} matching results to CP. Continue?`)) return;
              setImportResult(await apiCall('/api/profile-import/run', {
                method: 'POST',
                body: JSON.stringify({ matchingType, replaceExisting: true })
              }));
              toast.success('Golden profiles imported');
            }}>
              <Send className="h-4 w-4 mr-2" /> Send Golden Profiles to CP
            </Button>
          </div>
          {importResult && (
            <Alert>
              <CheckCircle2 className="h-4 w-4" />
              <AlertTitle>{importResult.importedCount} profiles imported</AlertTitle>
              <AlertDescription>{importResult.durationMs}ms · errors: {importResult.errorCount}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {/* STEP 2 — required for Calculated Attributes to populate */}
      <Card className="mt-6">
        <CardHeader>
          <CardTitle>Step 2 · Reservation / Folio (transactional data)</CardTitle>
          <CardDescription>PostgreSQL → CP child instances. Calculated Attribute values are populated only after this data is loaded.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button onClick={async () => {
            await apiCall('/api/cp-data-import/run', { method: 'POST' });
            toast.info('Background import started (3-10 min)');
          }}>Send Reservation/Folio to CP</Button>
          {cpDataStatus && (
            <p className="mt-3 text-sm text-muted-foreground">
              Last run: {cpDataStatus.reservationCount} reservations + {cpDataStatus.folioCount} folios
              ({cpDataStatus.unmatchedGuestIds} unmatched)
            </p>
          )}
        </CardContent>
      </Card>
    </>
  );
}
```

## ProfileViewPage — display Calculated Attributes

Show the Calculated Attribute values as a separate section on the GuestProfile detail page. **But also display the note that values are populated only after Step 2 (Reservation/Folio import) finishes.**

```tsx
{calcAttrs && Object.keys(calcAttrs).length > 0 ? (
  <Card>
    <CardHeader>
      <CardTitle>Calculated Attributes</CardTitle>
      <CardDescription>
        Aggregate values based on Reservation/Folio instances. An empty value means Step 2 (Send to CP) has not been run.
      </CardDescription>
    </CardHeader>
    <CardContent className="grid grid-cols-2 md:grid-cols-3 gap-3">
      {Object.entries(calcAttrs).map(([k, v]) => (
        <div key={k} className="rounded-md border p-3">
          <div className="text-xs text-muted-foreground">{ATTR_LABEL[k] ?? k}</div>
          <div className="text-lg font-semibold">{formatValue(v)}</div>
        </div>
      ))}
    </CardContent>
  </Card>
) : (
  <Alert>
    <AlertTriangle className="h-4 w-4" />
    <AlertTitle>No Calculated Attribute values</AlertTitle>
    <AlertDescription>
      On the "Send to CP" screen, run Step 2 (Reservation/Folio import), then wait until CP
      finishes indexing (a few minutes to tens of minutes). The values appear once the defined
      attribute's Status becomes `COMPLETED`.
    </AlertDescription>
  </Alert>
)}
```

## AiRulesPage — HITL (shadcn Dialog)

Generate → preview → Approve/Reject flow. Place an AI model selector in the header (so the user can switch models while seeing the cost/quality trade-off):

```tsx
<Select value={modelId} onValueChange={setModelId}>
  <SelectTrigger className="w-72"><SelectValue /></SelectTrigger>
  <SelectContent>
    <SelectItem value="us.anthropic.claude-opus-4-7">Claude Opus 4.7 (best, 1M ctx)</SelectItem>
    <SelectItem value="anthropic.claude-sonnet-4-20250514-v1:0">Claude Sonnet 4 (balanced)</SelectItem>
    <SelectItem value="us.anthropic.claude-opus-4-6">Claude Opus 4.6 (1M ctx)</SelectItem>
    <SelectItem value="anthropic.claude-haiku-4-5-20251001">Claude Haiku 4.5 (cheap)</SelectItem>
  </SelectContent>
</Select>
```

Send `modelId` along in the request body, and the Lambda passes that value to Bedrock InvokeModel.

## Per-domain customization points

| Industry | Dashboard specialization | Profile specialization | Additional pages |
|------|-------------|-----------|-----------|
| Airline | Revenue by route, FFP distribution | Travel journey timeline | Mileage dashboard |
| Hotel | Room occupancy, ADR trend | Stay history + Calculated Attributes | Loyalty |
| Retail | GMV, category distribution | Purchase funnel, RFM | Cart analysis |
| Finance | AUM, transaction frequency | Product portfolio | Risk score |
| Telecom | ARPU, churn rate | Plan history | Network quality |

## How to add shadcn components

Create each component directly under `components/ui/*.tsx`. If the shadcn CLI is available, you can do it all at once with `npx shadcn-ui@latest add card button badge alert table select tabs dialog skeleton`.

If manual creation is needed, copy the code from `https://ui.shadcn.com/docs/components/<name>` as-is. (This skill can also use the `frontend/src/components/ui/*` of the hotel-1 project as a reference template without any external fetch.)
