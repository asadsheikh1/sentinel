import { Link } from 'react-router'

export function NotFoundPage() {
  return (
    <div className="space-y-2">
      <h1 className="text-xl font-semibold">Not found</h1>
      <Link to="/incidents" className="text-sm text-accent">
        Back to incidents
      </Link>
    </div>
  )
}
