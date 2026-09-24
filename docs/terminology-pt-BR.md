# Terminologia de produto (pt-BR)

Única referência de terminologia do Clinic OS / Clinic Ops. Voz: direta,
acolhedora e objetiva, alinhada a “Mais tempo para o que importa”. Use verbos
que descrevem a tarefa; explique o problema e a próxima ação segura. Não
explique banco de dados, transporte HTTP ou detalhes de implementação ao usuário.
A marca permanece Clinic OS até a decisão de nome do produto.

| Conceito | Texto na interface |
| --- | --- |
| clinic | clínica |
| staff | equipe da clínica |
| physician / practitioner (catálogo médico atual) | médico |
| patient | paciente |
| patient registry | cadastro de pacientes |
| register patient | cadastrar paciente |
| appointment / booking | consulta / agendamento |
| book / reschedule / cancel | agendar / reagendar / cancelar |
| encounter (registro clínico, não o agendamento) | atendimento |
| availability block / window | período de disponibilidade |
| retire availability | encerrar disponibilidade |
| day / week agenda | agenda diária / semanal |
| scheduled / cancelled | agendada / cancelada |
| patient_request / clinic_request | solicitação do paciente / da clínica |
| practitioner_unavailable | médico indisponível |
| duplicate / other | duplicidade / outro motivo |
| authenticator | aplicativo autenticador |
| authentication code | código de autenticação |
| two-step / step-up verification | verificação em duas etapas / confirmação de segurança |
| sign in / sign out | entrar / sair |

## Formatos e limites

- Datas de leitura: DD/MM/AAAA; hora: HH:MM, 24 horas. O horário da clínica
  vem do fuso IANA configurado, nunca do fuso do navegador. Armazenamento UTC
  e conversão de horário ambíguo/inexistente não mudam.
- Controles nativos `date` e `datetime-local` continuam enviando ISO; o navegador
  apresenta o controle no idioma do usuário. Datas ISO nos campos ocultos,
  URLs e contratos não são texto de apresentação.
- Valores em reais: `R$ 1.234,56`. Não há cobrança nem campo monetário de produto
  nesta entrega. O exemplo sintético apenas formata Decimal; não altera parsing
  numérico existente. Não converter formatos mistos ou inferir separadores.
- Nomes mantêm acentos, com a normalização Unicode existente; nunca traduzir
  nomes próprios, identificadores médicos ou conteúdo informado pelo usuário.
- Valores de enums, URLs, APIs, códigos de erro e vocabulário de auditoria
  permanecem em inglês. Rótulos traduzidos ficam na camada de apresentação,
  sem migrações de dados ou de schema.
- Falhas de autenticação não revelam a existência da conta; códigos aceitos
  não podem ser reutilizados. Erros de agenda preservam as exigências de data
  futura, mesmo dia local, disponibilidade e ausência de conflitos.
- Catálogo: `locale/pt_BR/LC_MESSAGES/django.po` (e `.mo` distribuído). Compilar
  com `python manage.py compilemessages -l pt_BR` após alterar traduções.
