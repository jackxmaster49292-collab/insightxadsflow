import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, newIdempotencyKey } from './client'
import type {
  Accepted,
  AuditEvent,
  Chat,
  Connection,
  ForwardingEvent,
  Me,
  Rule,
  RuleJob,
  RuleListItem,
  UsageSummary,
} from './types'

export const keys = {
  me: ['me'] as const,
  connections: ['connections'] as const,
  connection: (id: string) => ['connections', id] as const,
  chats: (query: string) => ['chats', query] as const,
  rules: ['rules'] as const,
  rule: (id: string) => ['rules', id] as const,
  ruleEvents: (id: string) => ['rules', id, 'events'] as const,
  ruleJobs: (id: string) => ['rules', id, 'jobs'] as const,
  activity: ['activity'] as const,
  usage: (period: string) => ['usage', period] as const,
  audit: ['audit'] as const,
}

export const useMe = () => useQuery({ queryKey: keys.me, queryFn: () => api.get<Me>('/me'), retry: false })

export const useConnections = () =>
  useQuery({ queryKey: keys.connections, queryFn: () => api.get<Connection[]>('/telegram/connections') })

export const useConnection = (id: string) =>
  useQuery({
    queryKey: keys.connection(id),
    queryFn: () => api.get<Connection>(`/telegram/connections/${id}`),
    enabled: Boolean(id),
  })

export const useChats = (params: Record<string, string> = {}) => {
  const query = new URLSearchParams(params).toString()
  return useQuery({
    queryKey: keys.chats(query),
    queryFn: () => api.get<Chat[]>(`/telegram/chats${query ? `?${query}` : ''}`),
  })
}

export const useRules = () =>
  useQuery({ queryKey: keys.rules, queryFn: () => api.get<RuleListItem[]>('/forwarding-rules') })

export const useRule = (id: string) =>
  useQuery({
    queryKey: keys.rule(id),
    queryFn: () => api.get<Rule>(`/forwarding-rules/${id}`),
    enabled: Boolean(id),
  })

export const useRuleEvents = (id: string) =>
  useQuery({
    queryKey: keys.ruleEvents(id),
    queryFn: () => api.get<ForwardingEvent[]>(`/forwarding-rules/${id}/events?limit=100`),
    enabled: Boolean(id),
    // The panel is a monitoring surface; keep it reasonably fresh.
    refetchInterval: 15_000,
  })

export const useRuleJobs = (id: string) =>
  useQuery({
    queryKey: keys.ruleJobs(id),
    queryFn: () => api.get<RuleJob[]>(`/forwarding-rules/${id}/jobs`),
    enabled: Boolean(id),
    refetchInterval: 15_000,
  })

export const useActivity = () =>
  useQuery({
    queryKey: keys.activity,
    queryFn: () => api.get<ForwardingEvent[]>('/activity?limit=50'),
    refetchInterval: 15_000,
  })

export const useUsage = (period = '24h') =>
  useQuery({ queryKey: keys.usage(period), queryFn: () => api.get<UsageSummary>(`/usage/summary?period=${period}`) })

export const useAuditEvents = () =>
  useQuery({ queryKey: keys.audit, queryFn: () => api.get<AuditEvent[]>('/audit-events') })

/** Control commands always carry an idempotency key so a retry is safe. */
export function useRuleCommand(ruleId: string, command: 'activate' | 'pause' | 'resume' | 'retry-failed') {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api.post<Accepted>(`/forwarding-rules/${ruleId}/${command}`, undefined, newIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.rule(ruleId) })
      void queryClient.invalidateQueries({ queryKey: keys.rules })
      void queryClient.invalidateQueries({ queryKey: keys.ruleJobs(ruleId) })
      void queryClient.invalidateQueries({ queryKey: keys.ruleEvents(ruleId) })
    },
  })
}

export function useSyncChats(connectionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<Accepted>(`/telegram/connections/${connectionId}/sync`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['chats'] })
      void queryClient.invalidateQueries({ queryKey: keys.connection(connectionId) })
    },
  })
}

export function useHealthCheck(connectionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<Accepted>(`/telegram/connections/${connectionId}/health-check`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.connection(connectionId) }),
  })
}

export function useDisconnect(connectionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (revoke: boolean) =>
      api.post<Accepted>(`/telegram/connections/${connectionId}/disconnect`, { revoke }, newIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.connections })
      void queryClient.invalidateQueries({ queryKey: keys.rules })
    },
  })
}

export function useDeleteRule() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (ruleId: string) => api.delete<void>(`/forwarding-rules/${ruleId}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.rules }),
  })
}
