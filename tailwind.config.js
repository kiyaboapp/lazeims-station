/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: ['./station/static/**/*.{html,js}'],
  safelist: [
    // Dynamic KPI card colors: text-${color}-600 dark:text-${color}-400
    ...['indigo', 'blue', 'green', 'purple', 'teal', 'yellow'].flatMap(c => [
      `text-${c}-600`,
      `text-${c}-400`,
      `dark:text-${c}-400`,
    ]),
    // Sidebar collapse (applied dynamically)
    'w-16', 'lg:ml-16',
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
