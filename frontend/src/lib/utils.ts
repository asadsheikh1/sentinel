import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

const absolute = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'medium',
})
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

/** "3 minutes ago", for a timestamp from the API. */
export function ago(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000)
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['day', 86_400],
    ['hour', 3_600],
    ['minute', 60],
  ]
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit)
  }
  return relative.format(seconds, 'second')
}

export function when(iso: string): string {
  return absolute.format(new Date(iso))
}
