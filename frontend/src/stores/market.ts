/**
 * 全局状态：免责声明、交易时段、LLM 可用性。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { getAnalysisStatus } from '../api'

export const useMarketStore = defineStore('market', () => {
  const disclaimer = ref(
    '本系统仅供学习研究使用，所有数据与建议不构成任何投资建议。股市有风险，投资需谨慎。'
  )
  const trading = ref(false)
  const llmAvailable = ref(false)
  const llmModel = ref('rule-based')

  async function fetchAnalysisStatus() {
    try {
      const data = await getAnalysisStatus()
      llmAvailable.value = data.llm_available
      llmModel.value = data.model
    } catch {
      llmAvailable.value = false
    }
  }

  return { disclaimer, trading, llmAvailable, llmModel, fetchAnalysisStatus }
})
