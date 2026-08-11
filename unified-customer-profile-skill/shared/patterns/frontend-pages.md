# Frontend Page Patterns

**Stack**: React 18 + Vite + TypeScript + Tailwind v3 + shadcn/ui + Radix primitives + lucide-react + recharts + sonner. **Cloudscape is not used.**

## ⚠️ 디자인 원칙 (필수)

1. **액센트 1색**: `primary` 색상 하나 + neutral 계열(background, foreground, muted, border)만 사용. 원색 여러 개 혼합 금지.
2. **색은 정보에만**: Badge/Status/Alert 등 의미 전달 목적에만 색 사용. 장식 목적 금지.
3. **장식 그라데이션 금지**: 제목 텍스트 그라데이션, 배경 글로우, 컬러 보더 그라데이션 사용하지 않음.
4. shadcn/ui의 CSS variable 테마를 그대로 사용하면 위 원칙이 자연스럽게 충족됨.

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
import { LayoutDashboard, Database, Network, GaugeCircle, Sparkles, Users2, GitGraph, Send, ListChecks } from 'lucide-react';
import Layout from './components/Layout';
import AuthGate from './components/AuthGate';
import DashboardPage from './pages/DashboardPage';
import WorkflowPage from './pages/WorkflowPage';
// ... other pages

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/workflow', label: 'Demo Workflow', icon: ListChecks },   // 데모 스테퍼 (필수)
  { to: '/ingestion', label: 'Data Ingestion', icon: Database },
  { to: '/matching', label: 'Entity Matching', icon: Network },
  { to: '/accuracy', label: 'Accuracy', icon: GaugeCircle },
  { to: '/ai-rules', label: 'AI Rules', icon: Sparkles },
  { to: '/profile-import', label: 'Send to CP', icon: Send },
  { to: '/profiles', label: 'Unified Profile', icon: Users2 },
  { to: '/graph', label: 'Knowledge Graph', icon: GitGraph },     // optional
];

export default function App() {
  return (
    <AuthProvider {...oidcConfig}>
      <AuthGate>
        <BrowserRouter>
          <Layout nav={NAV}>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/workflow" element={<WorkflowPage />} />
              {/* ... other routes */}
              <Route path="*" element={<Navigate to="/" />} />
            </Routes>
          </Layout>
          <Toaster position="top-right" richColors />
        </BrowserRouter>
      </AuthGate>
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

## AuthGate — 인증 상태 래퍼 (필수)

인증 실패 시 "Failed to fetch" 대신 명확한 로그인 UI를 표시합니다. `App.tsx`에서 모든 라우트를 감쌉니다.

```tsx
// src/components/AuthGate.tsx
import { useAuth } from 'react-oidc-context';
import { Button } from './ui/button';
import { Alert, AlertTitle, AlertDescription } from './ui/alert';
import { LogIn, Loader2 } from 'lucide-react';

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const auth = useAuth();

  if (auth.isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (auth.error) {
    return (
      <div className="flex items-center justify-center min-h-screen p-6">
        <Alert variant="destructive" className="max-w-md">
          <AlertTitle>Authentication Error</AlertTitle>
          <AlertDescription className="mt-2">
            {auth.error.message}
            <Button className="mt-4 w-full" onClick={() => auth.signinRedirect()}>
              <LogIn className="h-4 w-4 mr-2" /> Sign in again
            </Button>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-center space-y-4">
          <h2 className="text-xl font-semibold">Sign in required</h2>
          <p className="text-muted-foreground">Please sign in to access the application.</p>
          <Button onClick={() => auth.signinRedirect()}>
            <LogIn className="h-4 w-4 mr-2" /> Sign in with SSO
          </Button>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
```

`App.tsx`에서 사용:

```tsx
<AuthProvider {...oidcConfig}>
  <AuthGate>
    <BrowserRouter>
      <Layout nav={NAV}>...</Layout>
    </BrowserRouter>
  </AuthGate>
</AuthProvider>
```

## auth.ts — singleton userManager (필수)

`userManager`를 여러 곳에서 `new`하면 세션 충돌이 발생합니다. 반드시 하나만 export합니다.

```typescript
// src/api/auth.ts
import { UserManager, WebStorageStateStore } from 'oidc-client-ts';

const oidcConfig = {
  authority: `https://cognito-idp.${import.meta.env.VITE_REGION}.amazonaws.com/${import.meta.env.VITE_COGNITO_USER_POOL_ID}`,
  client_id: import.meta.env.VITE_COGNITO_CLIENT_ID,
  redirect_uri: `${window.location.origin}/callback`,
  post_logout_redirect_uri: window.location.origin,
  response_type: 'code',
  scope: 'openid email',
  userStore: new WebStorageStateStore({ store: window.localStorage }),
};

// ⚠️ Singleton — 전체 앱에서 이 인스턴스만 사용
export const userManager = new UserManager(oidcConfig);
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

## WorkflowPage — 데모 스테퍼 (필수)

6개 서비스의 실행 순서와 의존관계를 시각적으로 안내합니다. 평평한 메뉴 구조에서는 무엇을 먼저 실행해야 하는지 알 수 없으므로, 이 페이지가 데모 진행의 나침반 역할을 합니다.

```tsx
import { CheckCircle2, Circle, Loader2, ChevronRight } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

interface Step {
  id: string;
  label: string;
  description: string;
  route: string;
  checkEndpoint: string; // GET → { completed: boolean }
}

const STEPS: Step[] = [
  { id: 'ingest', label: '1. Data Ingestion', description: 'S3에 원본 데이터 적재 + Glue Table 생성', route: '/ingestion', checkEndpoint: '/api/ingestion/status' },
  { id: 'rules', label: '2. AI Rule Generation', description: 'Bedrock로 매칭 규칙 생성 + HITL 승인', route: '/ai-rules', checkEndpoint: '/api/rules' },
  { id: 'matching', label: '3. Entity Matching', description: '3가지 전략 순차 실행 + 비교', route: '/matching', checkEndpoint: '/api/matching/status?type=advanced' },
  { id: 'import', label: '4. Send to CP', description: 'Golden Profile + Reservation/Folio → Customer Profiles', route: '/profile-import', checkEndpoint: '/api/profile-import/status' },
  { id: 'profile', label: '5. Unified Profile', description: '통합 프로필 + Calculated Attributes 확인', route: '/profiles', checkEndpoint: '/api/profiles/search?key=email&value=check' },
  { id: 'graph', label: '6. Knowledge Graph (Optional)', description: 'Neptune 동기화 + Graph RAG 질의', route: '/graph', checkEndpoint: '/api/graph/status' },
];

export default function WorkflowPage() {
  const navigate = useNavigate();
  const [statuses, setStatuses] = useState<Record<string, 'pending' | 'done' | 'running'>>({});

  useEffect(() => {
    // 각 단계 완료 여부 확인
    STEPS.forEach(async (step) => {
      try {
        const res = await apiCall<{ completed?: boolean; status?: string }>(step.checkEndpoint);
        const done = res.completed || res.status === 'SUCCEEDED' || res.status === 'COMPLETED';
        setStatuses(prev => ({ ...prev, [step.id]: done ? 'done' : 'pending' }));
      } catch {
        setStatuses(prev => ({ ...prev, [step.id]: 'pending' }));
      }
    });
  }, []);

  const currentStep = STEPS.findIndex(s => statuses[s.id] !== 'done');

  return (
    <>
      <PageHeader title="Demo Workflow"
        description="단계별로 진행하세요. 각 단계는 이전 단계의 결과에 의존합니다." />

      <div className="space-y-3">
        {STEPS.map((step, i) => {
          const status = statuses[step.id] ?? 'pending';
          const isCurrent = i === currentStep;
          return (
            <Card
              key={step.id}
              className={cn(
                'cursor-pointer transition-colors hover:bg-muted/50',
                isCurrent && 'ring-2 ring-primary'
              )}
              onClick={() => navigate(step.route)}
            >
              <CardContent className="flex items-center gap-4 py-4">
                {status === 'done' ? (
                  <CheckCircle2 className="h-6 w-6 text-green-600 shrink-0" />
                ) : isCurrent ? (
                  <Circle className="h-6 w-6 text-primary shrink-0" />
                ) : (
                  <Circle className="h-6 w-6 text-muted-foreground/40 shrink-0" />
                )}
                <div className="flex-1 min-w-0">
                  <p className={cn('font-medium text-sm', status === 'done' && 'text-muted-foreground')}>
                    {step.label}
                  </p>
                  <p className="text-xs text-muted-foreground">{step.description}</p>
                </div>
                <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* 전체 진행률 */}
      <div className="mt-6 text-sm text-muted-foreground text-center">
        {Object.values(statuses).filter(s => s === 'done').length} / {STEPS.length} steps completed
      </div>
    </>
  );
}
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

## MatchingComparisonPage — 개별 실행 + 규칙 표시 + 비교 테이블 + AI 추천 (필수)

> ⚠️ ER 동시 실행 쿼터: 리전당 1개. `Promise.all` 사용 금지.
> 각 전략을 **개별적으로** 실행할 수 있되, 동시에 두 개를 실행할 수는 없습니다.

### 필수 UI 요소

1. **전략별 카드**: 각 전략(simple/advanced/ml)마다 독립 카드 — 규칙 내용 표시, 개별 실행 버튼, 개별 결과
2. **쿼터 배너**: 동시 실행 불가 안내
3. **비교 테이블**: 완료된 전략들을 나란히 비교 (원본/그룹/매칭/미매칭/중복제거율)
4. **AI 추천**: 2개 이상 결과가 있으면 Bedrock 기반 전략 추천

### 백엔드 추가 라우트

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/matching/rules?type=<matchingType>` | 해당 전략의 규칙 목록 조회 |

```typescript
// GET /api/matching/rules?type=simple — 전략별 규칙 조회
async function getMatchingRules(matchingType: string) {
  const workflowName = `${WORKFLOW_NAME}-${matchingType}`;
  const { rules, resolutionType } = await erClient.send(
    new GetMatchingWorkflowCommand({ workflowName })
  );
  // rules: [{ ruleName: 'NameAndEmail', matchingKeys: ['name','email'] }, ...]
  return { matchingType, resolutionType, rules: rules ?? [] };
}
```

### 프론트엔드

```tsx
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { Loader2, AlertTriangle, Sparkles, Play, CheckCircle2, List } from 'lucide-react';

type MatchingType = 'simple' | 'advanced' | 'ml';

interface MatchingRule {
  ruleName: string;
  matchingKeys: string[];
}

interface MatchingSummary {
  matchingType: MatchingType;
  totalVariants: number;
  groups: number;
  matched: number;
  unmatched: number;
  deduplicationRate: string;
}

interface StrategyState {
  type: MatchingType;
  label: string;
  description: string;
  rules: MatchingRule[];
  rulesLoading: boolean;
  status: 'idle' | 'running' | 'succeeded' | 'failed';
  result: MatchingSummary | null;
}

const INITIAL_STRATEGIES: StrategyState[] = [
  { type: 'simple', label: 'Simple Rule', description: '단일 키 매칭 (예: 이메일만)', rules: [], rulesLoading: false, status: 'idle', result: null },
  { type: 'advanced', label: 'Advanced Rule', description: '복합 키 조합 (예: 이름+이메일, 전화+생년월일)', rules: [], rulesLoading: false, status: 'idle', result: null },
  { type: 'ml', label: 'ML Matching', description: '머신러닝 기반 유사도 매칭 (규칙 없음, 학습 데이터 기반)', rules: [], rulesLoading: false, status: 'idle', result: null },
];

export default function MatchingComparisonPage() {
  const [strategies, setStrategies] = useState<StrategyState[]>(INITIAL_STRATEGIES);
  const [busy, setBusy] = useState(false); // 리전 전체에서 실행 중인 job 있음
  const [recommendation, setRecommendation] = useState<string | null>(null);
  const [loadingReco, setLoadingReco] = useState(false);

  // 페이지 로드 시: 각 전략의 규칙 + 기존 결과 로드
  useEffect(() => {
    strategies.forEach((s, i) => {
      loadRules(s.type, i);
      loadExistingResult(s.type, i);
    });
    checkBusy();
  }, []);

  async function loadRules(type: MatchingType, idx: number) {
    updateStrategy(idx, { rulesLoading: true });
    try {
      const res = await apiCall<{ rules: MatchingRule[] }>(`/api/matching/rules?type=${type}`);
      updateStrategy(idx, { rules: res.rules, rulesLoading: false });
    } catch {
      updateStrategy(idx, { rulesLoading: false });
    }
  }

  async function loadExistingResult(type: MatchingType, idx: number) {
    try {
      const res = await apiCall<{ summary: MatchingSummary }>(`/api/matching/results?type=${type}`);
      if (res.summary.groups > 0) {
        updateStrategy(idx, { result: res.summary, status: 'succeeded' });
      }
    } catch { /* no prior results */ }
  }

  async function checkBusy() {
    const res = await apiCall<{ busy: boolean; runningType?: string }>('/api/matching/running');
    setBusy(res.busy);
    if (res.busy && res.runningType) {
      const idx = strategies.findIndex(s => s.type === res.runningType);
      if (idx >= 0) updateStrategy(idx, { status: 'running' });
    }
  }

  // 개별 전략 실행
  async function runSingle(type: MatchingType) {
    const idx = strategies.findIndex(s => s.type === type);

    // 쿼터 사전 체크
    const check = await apiCall<{ busy: boolean; runningType?: string }>('/api/matching/running');
    if (check.busy) {
      toast.error(`다른 매칭(${check.runningType})이 실행 중입니다. 완료 후 다시 시도하세요.`);
      return;
    }

    updateStrategy(idx, { status: 'running', result: null });
    setBusy(true);

    // 매칭 시작
    await apiCall('/api/matching/run', {
      method: 'POST',
      body: JSON.stringify({ matchingType: type, wait: false }),
    });

    // 폴링 (10초 간격)
    const poll = async () => {
      const res = await apiCall<{ status: string }>(`/api/matching/status?type=${type}`);
      if (res.status === 'RUNNING') {
        setTimeout(poll, 10000);
      } else {
        // 완료 — 결과 조회
        const result = await apiCall<{ summary: MatchingSummary }>(`/api/matching/results?type=${type}`);
        updateStrategy(idx, { status: res.status === 'SUCCEEDED' ? 'succeeded' : 'failed', result: result.summary });
        setBusy(false);
        toast.success(`${type} 매칭 완료`);
      }
    };
    poll();
  }

  // 전체 순차 실행
  async function runAll() {
    for (const s of strategies) {
      if (s.status === 'succeeded') continue; // 이미 완료된 건 스킵
      await runSingleAndWait(s.type);
    }
  }

  async function runSingleAndWait(type: MatchingType) {
    const idx = strategies.findIndex(s => s.type === type);
    const check = await apiCall<{ busy: boolean }>('/api/matching/running');
    if (check.busy) {
      // 다른 job이 끝날 때까지 대기
      await new Promise(r => setTimeout(r, 10000));
      return runSingleAndWait(type);
    }

    updateStrategy(idx, { status: 'running', result: null });
    setBusy(true);

    await apiCall('/api/matching/run', {
      method: 'POST',
      body: JSON.stringify({ matchingType: type, wait: false }),
    });

    // 동기 대기
    let status = 'RUNNING';
    while (status === 'RUNNING') {
      await new Promise(r => setTimeout(r, 10000));
      const res = await apiCall<{ status: string }>(`/api/matching/status?type=${type}`);
      status = res.status;
    }

    const result = await apiCall<{ summary: MatchingSummary }>(`/api/matching/results?type=${type}`);
    updateStrategy(idx, { status: 'succeeded', result: result.summary });
    setBusy(false);
  }

  function updateStrategy(idx: number, patch: Partial<StrategyState>) {
    setStrategies(prev => prev.map((s, i) => i === idx ? { ...s, ...patch } : s));
  }

  const completedResults = strategies.filter(s => s.result).map(s => s.result!);

  // AI 추천 요청
  async function getRecommendation() {
    if (completedResults.length < 2) return;
    setLoadingReco(true);
    try {
      const res = await apiCall<{ recommendation: string; status?: string; requestId?: string }>(
        '/api/matching/recommend-latest'
      );
      if (res.status === 'PROCESSING') {
        // 시작+폴링 (29초 대응)
        const pollReco = async () => {
          const poll = await apiCall<{ status: string; recommendation?: string }>(
            `/api/matching/recommend-latest?id=${res.requestId}`
          );
          if (poll.status === 'COMPLETED') {
            setRecommendation(poll.recommendation!);
            setLoadingReco(false);
          } else {
            setTimeout(pollReco, 10000);
          }
        };
        pollReco();
      } else {
        setRecommendation(res.recommendation);
        setLoadingReco(false);
      }
    } catch {
      setLoadingReco(false);
      toast.error('추천 생성에 실패했습니다');
    }
  }

  return (
    <>
      <PageHeader title="Matching Comparison"
        description="3가지 매칭 전략을 개별 또는 순차 실행하고 결과를 비교합니다"
        actions={
          <Button onClick={runAll} disabled={busy} variant="secondary">
            Run All (Sequential)
          </Button>
        } />

      {/* 쿼터 안내 배너 */}
      <Alert className="mb-4">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>Entity Resolution Quota</AlertTitle>
        <AlertDescription>
          리전당 동시 매칭 작업 1개만 실행 가능합니다 (조정 불가). 하나가 완료된 후 다음을 실행하세요.
        </AlertDescription>
      </Alert>

      {/* ─── 전략별 카드 (개별 실행 + 규칙 + 결과) ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        {strategies.map(s => (
          <Card key={s.type} className={cn(s.status === 'running' && 'ring-2 ring-primary')}>
            <CardHeader>
              <div className="flex items-center justify-between">
                <Badge variant="secondary">{s.label}</Badge>
                {s.status === 'succeeded' && <CheckCircle2 className="h-4 w-4 text-green-600" />}
                {s.status === 'running' && <Loader2 className="h-4 w-4 animate-spin text-primary" />}
              </div>
              <CardDescription>{s.description}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {/* 규칙 표시 */}
              <div>
                <p className="text-xs font-medium text-muted-foreground flex items-center gap-1 mb-1">
                  <List className="h-3 w-3" /> Rules
                </p>
                {s.rulesLoading ? (
                  <Skeleton className="h-4 w-full" />
                ) : s.rules.length > 0 ? (
                  <ul className="text-xs space-y-1">
                    {s.rules.map(rule => (
                      <li key={rule.ruleName} className="flex items-center gap-1.5">
                        <span className="font-mono bg-muted px-1.5 py-0.5 rounded">{rule.ruleName}</span>
                        <span className="text-muted-foreground">
                          ({rule.matchingKeys.join(' + ')})
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-muted-foreground italic">
                    {s.type === 'ml' ? 'ML 모델 기반 (명시적 규칙 없음)' : '규칙 미설정'}
                  </p>
                )}
              </div>

              {/* 개별 결과 */}
              {s.result && (
                <div className="border-t pt-3 space-y-1">
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                    <span className="text-muted-foreground">원본 레코드</span>
                    <span className="text-right font-medium">{s.result.totalVariants}</span>
                    <span className="text-muted-foreground">그룹 수</span>
                    <span className="text-right font-medium">{s.result.groups}</span>
                    <span className="text-muted-foreground">매칭</span>
                    <span className="text-right font-medium">{s.result.matched}</span>
                    <span className="text-muted-foreground">미매칭</span>
                    <span className="text-right font-medium">{s.result.unmatched}</span>
                  </div>
                  <div className="text-center pt-2">
                    <span className="text-lg font-bold">{s.result.deduplicationRate}</span>
                    <span className="text-xs text-muted-foreground ml-1">중복제거율</span>
                  </div>
                </div>
              )}

              {/* 실행 버튼 */}
              <Button
                className="w-full"
                size="sm"
                onClick={() => runSingle(s.type)}
                disabled={busy || s.status === 'running'}
              >
                {s.status === 'running' ? (
                  <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> Running...</>
                ) : s.status === 'succeeded' ? (
                  <><Play className="h-4 w-4 mr-2" /> Re-run</>
                ) : (
                  <><Play className="h-4 w-4 mr-2" /> Run {s.label}</>
                )}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ─── 비교 테이블 (2개 이상 결과 있을 때) ─── */}
      {completedResults.length >= 2 && (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle>전략별 비교</CardTitle>
            <CardDescription>완료된 전략들의 결과를 나란히 비교합니다</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>전략</TableHead>
                  <TableHead className="text-right">원본</TableHead>
                  <TableHead className="text-right">그룹</TableHead>
                  <TableHead className="text-right">매칭</TableHead>
                  <TableHead className="text-right">미매칭</TableHead>
                  <TableHead className="text-right">중복제거율</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {completedResults.map(r => (
                  <TableRow key={r.matchingType}>
                    <TableCell><Badge variant="secondary">{r.matchingType}</Badge></TableCell>
                    <TableCell className="text-right">{r.totalVariants}</TableCell>
                    <TableCell className="text-right">{r.groups}</TableCell>
                    <TableCell className="text-right">{r.matched}</TableCell>
                    <TableCell className="text-right">{r.unmatched}</TableCell>
                    <TableCell className="text-right font-semibold">{r.deduplicationRate}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* ─── AI 추천 ─── */}
      {completedResults.length >= 2 && (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Sparkles className="h-5 w-5" /> AI Recommendation
            </CardTitle>
            <CardDescription>
              Bedrock가 결과를 분석하여 최적 전략을 추천합니다
            </CardDescription>
          </CardHeader>
          <CardContent>
            {recommendation ? (
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{recommendation}</p>
            ) : (
              <Button variant="secondary" onClick={getRecommendation} disabled={loadingReco}>
                {loadingReco && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                결과 기반 전략 추천 받기
              </Button>
            )}
          </CardContent>
        </Card>
      )}

      {/* ─── 바 차트 ─── */}
      {completedResults.length > 0 && (
        <Card>
          <CardHeader><CardTitle>Deduplication Rate</CardTitle></CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={completedResults.map(r => ({
                type: r.matchingType,
                rate: parseFloat(r.deduplicationRate),
              }))}>
                <XAxis dataKey="type" /><YAxis unit="%" domain={[0, 100]} />
                <Tooltip />
                <Bar dataKey="rate" fill="hsl(var(--primary))" radius={[4,4,0,0]} />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}
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

## AiRulesPage — 시작+폴링 적용 (29초 벽 대응)

기존 AiRulesPage에서 Bedrock 호출 결과를 동기적으로 기다리면 API Gateway 29초 타임아웃에 걸립니다. 시작+폴링 패턴을 적용합니다.

```tsx
export default function AiRulesPage() {
  const [generating, setGenerating] = useState(false);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<any>(null);

  // 생성 시작 (즉시 반환)
  async function startGeneration() {
    setGenerating(true);
    setSuggestion(null);
    const res = await apiCall<{ requestId: string; status: string; result?: any; cached?: boolean }>(
      '/api/ai-agent/start', { method: 'POST', body: JSON.stringify({ modelId }) }
    );

    // 캐시 히트면 즉시 결과
    if (res.cached && res.result) {
      setSuggestion(res.result);
      setGenerating(false);
      return;
    }

    setRequestId(res.requestId);
    pollForResult(res.requestId);
  }

  // 10초 간격 폴링
  async function pollForResult(id: string) {
    const poll = async () => {
      const res = await apiCall<{ status: string; result?: any }>(`/api/ai-agent/latest-generation?id=${id}`);
      if (res.status === 'COMPLETED') {
        setSuggestion(res.result);
        setGenerating(false);
      } else if (res.status === 'FAILED') {
        toast.error('AI 규칙 생성에 실패했습니다');
        setGenerating(false);
      } else {
        setTimeout(poll, 10000);
      }
    };
    poll();
  }

  return (
    <>
      <PageHeader title="AI Rule Generation" />
      {/* model selector + generate button + HITL approve/reject dialog */}
      <Button onClick={startGeneration} disabled={generating}>
        {generating && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
        Generate Rules
      </Button>
      {generating && (
        <p className="text-sm text-muted-foreground mt-2">
          AI가 규칙을 생성 중입니다 (약 30-40초 소요)...
        </p>
      )}
      {/* suggestion preview + approve/reject */}
    </>
  );
}
```

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
