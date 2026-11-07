/**
 * 浏览器端 CSV 导出（带 BOM + 引号转义，Excel 直接打开不乱码）。
 *
 * 原来只有扫描命中表自己拼了一份 CSV；交易流水 / 回测明细 / 净值曲线 / 访客记录
 * 都带不走。统一走这里，避免每处重复实现「逗号、引号、换行」的转义。
 */
export function downloadCsv(
  filename: string,
  header: string[],
  rows: (string | number | boolean | null | undefined)[][],
): void {
  const esc = (v: string | number | boolean | null | undefined): string => {
    const s = v == null ? '' : String(v)
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const csv = '\uFEFF' + [header, ...rows].map((r) => r.map(esc).join(',')).join('\r\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 立刻 revoke 在部分浏览器会中断下载，交还对象交给下一个宏任务
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** 文件名用的当日 YYYYMMDD（本机时区，不用 ISO 的 UTC） */
export function csvDateTag(): string {
  const d = new Date()
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
}
