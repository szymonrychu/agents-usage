{{- define "agents-usage.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-usage.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "agents-usage.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-usage.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agents-usage.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agents-usage.labels" -}}
helm.sh/chart: {{ include "agents-usage.chart" . }}
{{ include "agents-usage.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Call with (dict "context" . "component" "api"). */}}
{{- define "agents-usage.componentLabels" -}}
{{ include "agents-usage.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "agents-usage.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agents-usage.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{/* imagePullSecrets from values plus global, deduplicated by name. */}}
{{- define "agents-usage.imagePullSecrets" -}}
{{- $g := ((.Values.global | default dict).imagePullSecrets) | default list }}
{{- $seen := dict }}
{{- $all := list }}
{{- range concat (.Values.imagePullSecrets | default list) $g }}
{{- if not (hasKey $seen .name) }}
{{- $_ := set $seen .name true }}
{{- $all = append $all . }}
{{- end }}
{{- end }}
{{- if $all }}
imagePullSecrets:
  {{- toYaml $all | nindent 2 }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding the database URL; fails when no source is set. */}}
{{- define "agents-usage.databaseSecretName" -}}
{{- $p := .Values.postgres -}}
{{- if and $p.url $p.existingSecret -}}
{{- fail "agents-usage: set only one of postgres.url and postgres.existingSecret" -}}
{{- else if $p.existingSecret -}}
{{- $p.existingSecret -}}
{{- else if $p.url -}}
{{- include "agents-usage.fullname" . }}-database
{{- else -}}
{{- fail "agents-usage: a Postgres connection is required: set postgres.url or postgres.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-usage.databaseSecretKey" -}}
{{- if .Values.postgres.existingSecret -}}
{{- .Values.postgres.existingSecretKey | default "uri" -}}
{{- else -}}
uri
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the claude OAuth token; fails when none is set. */}}
{{- define "agents-usage.claudeSecretName" -}}
{{- $c := .Values.claude -}}
{{- if and $c.token $c.existingSecret -}}
{{- fail "agents-usage: set only one of claude.token and claude.existingSecret" -}}
{{- else if $c.existingSecret -}}
{{- $c.existingSecret -}}
{{- else if $c.token -}}
{{- include "agents-usage.fullname" . }}-claude
{{- else -}}
{{- fail "agents-usage: the claude token is required: set claude.token or claude.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-usage.claudeSecretKey" -}}
{{- if .Values.claude.existingSecret -}}
{{- .Values.claude.existingSecretKey | default "CLAUDE_CODE_OAUTH_TOKEN" -}}
{{- else -}}
CLAUDE_CODE_OAUTH_TOKEN
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the orchestrator service token; fails when none is set. */}}
{{- define "agents-usage.orchestratorSecretName" -}}
{{- $o := .Values.orchestrator -}}
{{- if and $o.token $o.existingSecret -}}
{{- fail "agents-usage: set only one of orchestrator.token and orchestrator.existingSecret" -}}
{{- else if $o.existingSecret -}}
{{- $o.existingSecret -}}
{{- else if $o.token -}}
{{- include "agents-usage.fullname" . }}-orchestrator
{{- else -}}
{{- fail "agents-usage: the orchestrator token is required: set orchestrator.token or orchestrator.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-usage.orchestratorSecretKey" -}}
{{- if .Values.orchestrator.existingSecret -}}
{{- .Values.orchestrator.existingSecretKey | default "token" -}}
{{- else -}}
token
{{- end -}}
{{- end }}

{{/* Container env: the settings ConfigMap and the database URL from its Secret. Call with the root context. */}}
{{- define "agents-usage.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "agents-usage.fullname" . }}-settings
env:
  - name: USAGE_DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-usage.databaseSecretName" . }}
        key: {{ include "agents-usage.databaseSecretKey" . }}
{{- end }}

{{/* Validates Secret inputs even when no Deployment includes the env. */}}
{{- define "agents-usage.validate" -}}
{{- $_ := include "agents-usage.databaseSecretName" . }}
{{- $_ := include "agents-usage.claudeSecretName" . }}
{{- $_ := include "agents-usage.orchestratorSecretName" . }}
{{- end }}
