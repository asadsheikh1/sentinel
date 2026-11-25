import { Navigate, type RouteObject } from 'react-router'

import { RequireAuth } from '@/auth/auth'
import { Layout } from '@/components/Layout'
import { IncidentDetailPage } from '@/pages/IncidentDetailPage'
import { IncidentsPage } from '@/pages/IncidentsPage'
import { LoginPage } from '@/pages/LoginPage'
import { CataloguePage } from '@/pages/integrations/CataloguePage'
import { IntegrationDetailPage } from '@/pages/integrations/IntegrationDetailPage'
import { IntegrationsPage } from '@/pages/integrations/IntegrationsPage'
import { WizardPage } from '@/pages/integrations/WizardPage'
import { InventoryPage } from '@/pages/InventoryPage'
import { NotFoundPage } from '@/pages/NotFoundPage'

export const routes: RouteObject[] = [
  { path: '/login', element: <LoginPage /> },
  {
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/incidents" replace /> },
      { path: 'incidents', element: <IncidentsPage /> },
      { path: 'incidents/:id', element: <IncidentDetailPage /> },
      { path: 'integrations', element: <IntegrationsPage /> },
      { path: 'integrations/new', element: <CataloguePage /> },
      { path: 'integrations/new/:slug', element: <WizardPage /> },
      { path: 'integrations/:id', element: <IntegrationDetailPage /> },
      { path: 'inventory', element: <InventoryPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
