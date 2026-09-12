export async function fetchSeries({ ticker, start, end }) {
  const params = new URLSearchParams({ ticker, start, end })
  const response = await fetch(`/api/series?${params}`)
  const body = await response.json().catch(() => null)

  if (!response.ok) {
    const detail = body && body.detail ? body.detail : `HTTP ${response.status}`
    throw new Error(detail)
  }
  return body
}

export function formatPercent(fraction) {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return '--'
  const sign = fraction >= 0 ? '+' : ''
  return `${sign}${(fraction * 100).toFixed(2)}%`
}
