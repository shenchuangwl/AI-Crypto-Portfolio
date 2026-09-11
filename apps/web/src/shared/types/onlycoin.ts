export interface OnlyCoinMember {
  member_id: string; symbol: string; first_selected_at: string; first_available_at: string | null;
  first_scan_id: string; first_direction: string; directions: string[];
  parameter_version: string | null; rule_identity: Record<string, unknown> | null; provenance: unknown;
}
export interface OnlyCoinDaily {
  board_key: 'y'; business_date: string; cycle_start_utc: string; cycle_end_utc: string;
  server_time: string; projection_revision: string | number; as_of_scan_id: string | null;
  long: OnlyCoinMember[]; short: OnlyCoinMember[]; onlycoin: OnlyCoinMember[];
  counts: Record<string, number>; coverage: unknown; stale: boolean; as_of?: string;
}
export interface OnlyCoinLive extends OnlyCoinDaily {
  schema: 'onlycoin-live-v1'; version: string; symbols: string[];
  valid: boolean; status: 'live' | 'empty'; updated_at_utc: string; valid_until_utc: string;
}
export interface OnlyCoinSource {
  enabled: boolean; revision: number; generation: number; effective_state: string;
  consumer_connected: boolean; eligibility_only: true;
}
export interface OnlyCoinControl {
  enabled: boolean; expected_revision: number; request_id: string; reason: string;
}
