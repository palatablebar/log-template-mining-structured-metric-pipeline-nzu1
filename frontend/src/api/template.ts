import { api, queryString } from './client';
import type {
  SplitExecuteResult,
  SplitImpactPreview,
  SplitRollbackResult,
  Template,
  TemplateDetail,
  TemplateSplitJob,
} from '../types/models';

export const templateApi = {
  list: (params: { status?: string; source_id?: number; search?: string; include_noise?: boolean } = {}) =>
    api.get<{ items: Template[]; total: number }>(`/templates${queryString(params)}`),
  detail: (id: number) => api.get<TemplateDetail>(`/templates/${id}`),
  confirm: (id: number) => api.post<{ id: number; status: string; message: string }>(`/templates/${id}/confirm`),
  markNoise: (id: number) => api.post<{ id: number; status: string; message: string }>(`/templates/${id}/noise`),
  splitPreview: (id: number) => api.get<SplitImpactPreview>(`/templates/${id}/split-preview`),
  splitExecute: (id: number) => api.post<SplitExecuteResult>(`/templates/${id}/split`),
  splitRollback: (id: number) => api.post<SplitRollbackResult>(`/templates/${id}/split-rollback`),
  splitJobs: () => api.get<{ items: TemplateSplitJob[]; total: number }>(`/templates/split-jobs`),
};
