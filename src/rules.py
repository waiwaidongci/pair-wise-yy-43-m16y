from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
# 升级确认判据：catastrophic等级直接升级，或估算油量达到/超过升级线
ESCALATION_CRITERIA=(
    ('severity_catastrophic', '等级为catastrophic，必须升级'),
    ('quantity_at_threshold', '估算油量达到或超过升级线'),
)
def escalation_criterion(severity,quantity=0.0,threshold=1.0):
    if severity==SEVERITIES[-1]: return ESCALATION_CRITERIA[0][0]
    if threshold>0 and quantity>=threshold: return ESCALATION_CRITERIA[1][0]
    return None
def criterion_text(criterion):
    return dict(ESCALATION_CRITERIA).get(criterion, criterion)
# 确认失效原因
INVALID_REASON_QUANTITY_CORRECTED='quantity_corrected'
INVALID_REASONS={INVALID_REASON_QUANTITY_CORRECTED: '估算油量已被更正，确认自动作废'}
# 允许更正估算、确认升级的状态
ESTIMATE_EDIT_STATES=set(['reported','assessing'])
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def escalation_blocker(item,confirmation):
    """评估进入围控时的升级确认闸门；返回(卡点步骤,失效原因)，无问题返回None。"""
    criterion=escalation_criterion(item['severity'],item['quantity'],item['threshold'])
    if criterion is None: return None
    if confirmation is None:
        return ('escalation_confirmation', None)
    if confirmation['status']!='valid':
        return ('escalation_confirmation', confirmation.get('invalid_reason'))
    if confirmation['item_version']!=item['version']:
        return ('escalation_confirmation', INVALID_REASON_QUANTITY_CORRECTED)
    if confirmation['criterion']!=criterion:
        return ('escalation_confirmation', INVALID_REASON_QUANTITY_CORRECTED)
    if abs(float(confirmation['quantity'])-float(item['quantity']))>1e-9:
        return ('escalation_confirmation', INVALID_REASON_QUANTITY_CORRECTED)
    return None
