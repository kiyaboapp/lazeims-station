// shared/loading.js -- spinner and skeleton utilities

export function showSpinner(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="flex items-center justify-center py-12">
      <div class="relative">
        <div class="w-10 h-10 border-4 border-gray-200 dark:border-gray-700 rounded-full"></div>
        <div class="w-10 h-10 border-4 border-indigo-600 border-t-transparent rounded-full animate-spin absolute top-0 left-0"></div>
      </div>
    </div>`;
}

export function hideSpinner(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const spinner = el.querySelector('.animate-spin');
  if (spinner) spinner.closest('.flex')?.remove();
}

export function showSkeleton(containerId, rows = 5) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="animate-pulse space-y-3">
      ${Array.from({ length: rows }, () => `
        <div class="flex items-center gap-4">
          <div class="h-4 bg-gray-200 dark:bg-gray-700 rounded w-16"></div>
          <div class="h-4 bg-gray-200 dark:bg-gray-700 rounded flex-1"></div>
          <div class="h-4 bg-gray-200 dark:bg-gray-700 rounded w-20"></div>
        </div>
      `).join('')}
    </div>`;
}

export function showCardSkeleton(containerId, count = 3) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 animate-pulse">
      ${Array.from({ length: count }, () => `
        <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-5">
          <div class="h-4 bg-gray-200 dark:bg-gray-700 rounded w-3/4 mb-3"></div>
          <div class="h-3 bg-gray-200 dark:bg-gray-700 rounded w-1/2 mb-2"></div>
          <div class="h-3 bg-gray-200 dark:bg-gray-700 rounded w-2/3"></div>
        </div>
      `).join('')}
    </div>`;
}

export function showTableSkeleton(containerId, rows = 6, cols = 5) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <div class="animate-pulse">
      <div class="flex gap-4 mb-4 pb-3 border-b border-gray-200 dark:border-gray-700">
        ${Array.from({ length: cols }, () => '<div class="h-3 bg-gray-300 dark:bg-gray-600 rounded flex-1"></div>').join('')}
      </div>
      ${Array.from({ length: rows }, () => `
        <div class="flex gap-4 py-3">
          ${Array.from({ length: cols }, () => '<div class="h-3 bg-gray-200 dark:bg-gray-700 rounded flex-1"></div>').join('')}
        </div>
      `).join('')}
    </div>`;
}
