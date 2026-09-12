import { useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { fetchSeries, formatPercent } from './api.js'

const DEFAULT_START = '2015-01-01'
const today = () => new Date().toISOString().slice(0, 10)

// Stage 1a shows one ticker. The basis label is driven by the API's `basis`
// field rather than hardcoded, so Stage 1b changes what this says without a
// frontend edit.
const BASIS_LABEL = {
  price_return: 'Price return',
  total_return: 'Total return',
}

export default function App() {
  const [ticker, setTicker] = useState('AAPL')
  const [start, setStart] = useState(DEFAULT_START)
  const [end, setEnd] = useState(today())
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function onSubmit(event) {
    event.preventDefault()
    setLoading(true)
    setError(null)
    try {
      setData(await fetchSeries({ ticker: ticker.trim().toUpperCase(), start, end }))
    } catch (err) {
      setError(err.message)
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page">
      <header className="masthead">
        <h1>Sienner</h1>
        <p className="tagline">
          Portfolio evaluation. Stage 1a &mdash; one ticker, one line.
        </p>
      </header>

      <form className="controls" onSubmit={onSubmit}>
        <label>
          Ticker
          <input
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            maxLength={8}
            required
          />
        </label>
        <label>
          From
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} required />
        </label>
        <label>
          To
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} required />
        </label>
        <button type="submit" disabled={loading}>
          {loading ? 'Loading' : 'Run'}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {data && (
        <section className="result">
          <div className="headline">
            <div>
              <span className="metric-label">Total return</span>
              <span className="metric-value">{formatPercent(data.total_return)}</span>
            </div>
            <div>
              <span className="metric-label">Window</span>
              <span className="metric-value small">
                {data.effective.start} &rarr; {data.effective.end}
              </span>
            </div>
            <div>
              <span className="metric-label">Basis</span>
              <span className="metric-value small">
                {BASIS_LABEL[data.basis] || data.basis}
              </span>
            </div>
          </div>

          <p className="disclaimer">{data.disclaimer}</p>

          {data.warnings.map((warning) => (
            <p className="warning" key={warning}>
              {warning}
            </p>
          ))}

          <div className="chart">
            <ResponsiveContainer width="100%" height={380}>
              <LineChart data={data.points} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                <CartesianGrid stroke="#e6e3dd" vertical={false} />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 12, fill: '#6b6459' }}
                  minTickGap={48}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 12, fill: '#6b6459' }}
                  domain={['auto', 'auto']}
                  tickLine={false}
                  axisLine={false}
                  width={56}
                />
                <Tooltip
                  formatter={(value) => [value.toFixed(2), `Index (${data.base} at start)`]}
                  contentStyle={{
                    border: '1px solid #d9d4cb',
                    borderRadius: 6,
                    fontSize: 13,
                  }}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="#1c6758"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <p className="footnote">
            Indexed to {data.base} at {data.effective.start}. No dollar amounts &mdash;
            Sienner does not know your balance.
          </p>
        </section>
      )}
    </main>
  )
}
