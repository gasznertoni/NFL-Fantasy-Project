/**
 * Simple week dropdown + stepper (spec section 2). Range comes from the
 * data layer (lib/api.js), not hardcoded here.
 */
export default function WeekSelector({ week, minWeek, maxWeek, onChange }) {
  const weeks = []
  for (let w = minWeek; w <= maxWeek; w++) weeks.push(w)

  return (
    <div className="week-selector">
      <button
        type="button"
        className="week-step-button"
        onClick={() => onChange(week - 1)}
        disabled={week <= minWeek}
        aria-label="Previous week"
      >
        &larr;
      </button>
      <label className="week-select-label">
        Week
        <select value={week} onChange={(e) => onChange(Number(e.target.value))}>
          {weeks.map((w) => (
            <option key={w} value={w}>
              {w}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="week-step-button"
        onClick={() => onChange(week + 1)}
        disabled={week >= maxWeek}
        aria-label="Next week"
      >
        &rarr;
      </button>
    </div>
  )
}
