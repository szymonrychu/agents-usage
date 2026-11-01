{{/* Mount point of the claude OAuth token (prober only; the Secret key is projected to "token"). */}}
{{- define "agents-usage.claudeDir" -}}/var/run/secrets/claude{{- end }}

{{/* Mount point of the orchestrator service token (watcher only; projected to "token"). */}}
{{- define "agents-usage.orchestratorDir" -}}/var/run/secrets/orchestrator{{- end }}

{{/* Mount points of the service and ingest tokens files (api only; projected to "tokens"). */}}
{{- define "agents-usage.serviceTokensDir" -}}/var/run/secrets/service{{- end }}
{{- define "agents-usage.ingestTokensDir" -}}/var/run/secrets/ingest{{- end }}

{{/* Names of the listener tokens Secrets; fail when the listener is on without one. */}}
{{- define "agents-usage.serviceTokensSecret" -}}
{{- required "serviceListener.tokensSecret.name is required when serviceListener.enabled" .Values.serviceListener.tokensSecret.name -}}
{{- end }}
{{- define "agents-usage.ingestTokensSecret" -}}
{{- required "ingestListener.tokensSecret.name is required when ingestListener.enabled" .Values.ingestListener.tokensSecret.name -}}
{{- end }}

{{/* Settings ConfigMap data shared by every component; no secret values. Fails on a bad env key. */}}
{{- define "agents-usage.config" -}}
USAGE_HTTP_PORT: {{ .Values.components.api.port | int | toString | quote }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "USAGE_" $k) }}
{{- fail (printf "agents-usage: env key %q must start with USAGE_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/* Settings only one component reads, as container env entries. Call with (dict "context" . "component" "api"). */}}
{{- define "agents-usage.componentEnv" -}}
{{- $v := .context.Values }}
{{- if eq .component "api" }}
{{- if $v.serviceListener.enabled }}
- name: USAGE_SERVICE_PORT
  value: {{ $v.serviceListener.port | int | toString | quote }}
- name: USAGE_SERVICE_TOKENS_FILE
  value: {{ printf "%s/tokens" (include "agents-usage.serviceTokensDir" .context) | quote }}
{{- end }}
{{- if $v.ingestListener.enabled }}
- name: USAGE_INGEST_PORT
  value: {{ $v.ingestListener.port | int | toString | quote }}
- name: USAGE_INGEST_TOKENS_FILE
  value: {{ printf "%s/tokens" (include "agents-usage.ingestTokensDir" .context) | quote }}
{{- end }}
{{- else if eq .component "prober" }}
- name: USAGE_CLAUDE_TOKEN_FILE
  value: {{ printf "%s/token" (include "agents-usage.claudeDir" .context) | quote }}
{{- else if eq .component "watcher" }}
- name: USAGE_ORCHESTRATOR_URL
  value: {{ required "orchestrator.url is required" $v.orchestrator.url | quote }}
- name: USAGE_ORCHESTRATOR_TOKEN_FILE
  value: {{ printf "%s/token" (include "agents-usage.orchestratorDir" .context) | quote }}
{{- end }}
{{- end }}
