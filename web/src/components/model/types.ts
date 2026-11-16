/** Shared BYOK (Bring Your Own Key) and model types */

export type AccessType = 'api_key' | 'coding_plan' | 'oauth' | 'local';

/** Product plan families, distinct from the wire access type. */
export type PlanKind = 'token' | 'agent';

export interface PlanCapabilities {
  token_plan: boolean;
  agent_plan: boolean;
}

export interface ByokProvider {
  provider: string;
  display_name: string;
  access_type?: AccessType;
  brand_key?: string;
  parent_provider?: string;
  has_key: boolean;
  masked_key: string | null;
  base_url: string | null;
  is_custom?: boolean;
  use_response_api?: boolean;
}

export type SdkType = 'openai' | 'anthropic' | 'gemini' | 'codex' | 'deepseek' | 'qwq' | 'dashscope';

export interface RegionVariant {
  provider: string;
  display_name: string;
  region: string;
  sdk?: SdkType | string;
  base_url?: string | null;
  use_response_api?: boolean;
}

export interface ProviderCatalogEntry {
  provider: string;
  display_name: string;
  access_type: AccessType;
  brand_key: string;
  byok_eligible: boolean;
  region?: string;
  sdk?: SdkType | string;
  base_url?: string | null;
  use_response_api?: boolean;
  dynamic_models?: boolean;
  /** Retired upstream flow; hidden from the provider picker. */
  deprecated?: boolean;
  region_variants?: RegionVariant[];
  /** Product plan capability for the family (Token vs Agent plan). */
  plan_capabilities?: PlanCapabilities;
  /** API endpoint regions the family documents: cn, intl, global or local. */
  api_regions?: string[];
  /** For an access variant that is a named plan. */
  plan_kind?: PlanKind;
  plan_name?: string;
}

export interface CustomModelEntry {
  name: string;
  model_id: string;
  provider: string;
  parameters?: Record<string, unknown>;
  extra_body?: Record<string, unknown>;
  input_modalities?: string[];
}

export interface CustomModelFormState {
  name: string;
  model_id: string;
  provider: string;
  parameters: string;
  extra_body: string;
  input_modalities?: string[];
  _customProvider?: boolean;
}

export interface AddProviderFormState {
  name: string;
  parent_provider: string;
  api_key?: string;
  base_url?: string;
  use_response_api?: boolean;
}

export interface ProviderModelsData {
  models?: string[];
  display_name?: string;
}
