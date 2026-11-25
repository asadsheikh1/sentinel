import { Navigate, type RouteObject } from 'react-router'

import { RequireAuth } from '@/auth/auth'
import { Layout } from '@/components/Layout'
import { IncidentDetailPage } from '@/pages/IncidentDetailPage'
import { IncidentsPage } from '@/pages/IncidentsPage'
import { LoginPage } from '@/pages/LoginPage'
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
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
