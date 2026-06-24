(function () {
  const STEP_ORDER = ["intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select", "deploying"];
  const STEP_LABELS = {
    intent_parsing: "需求理解",
    architecture_planning: "架构规划",
    evaluate_candidates: "方案评估",
    confirm_and_select: "方案选择",
    deploying: "确认部署",
  };
  const PROGRESS_VARIANT_ORDER = ["a", "b", "d"];
  const PROGRESS_VARIANT_LABELS = {
    a: "A 箭头轨道",
    b: "B 脉冲线路",
    d: "D 输入框融合",
  };
  const DEFAULT_PROGRESS_UI = {
    variant: "b",
    activeStepIndex: null,
    a: {
      sweepMs: 1800,
    },
    b: {
      xPercent: 28,
      yPercent: 49,
      t1: 140,
      t2: 540,
      maxAmplitude: 9,
      pauseTime: 510,
    },
    d: {
      t1: 1800,
      t2: 300,
    },
  };
  const PROGRESS_PARAM_DEFS = {
    a: [
      { key: "sweepMs", label: "扫光周期", min: 800, max: 2800, step: 100, unit: "ms" },
    ],
    b: [
      { key: "xPercent", label: "X", min: 6, max: 38, step: 1, unit: "%" },
      { key: "yPercent", label: "Y", min: 20, max: 90, step: 1, unit: "%" },
      { key: "t1", label: "T1", min: 80, max: 700, step: 20, unit: "ms" },
      { key: "t2", label: "T2", min: 160, max: 1400, step: 20, unit: "ms" },
      { key: "maxAmplitude", label: "最大振幅", min: 8, max: 22, step: 1, unit: "" },
      { key: "pauseTime", label: "停顿时间", min: 120, max: 1200, step: 30, unit: "ms" },
    ],
    d: [
      { key: "t1", label: "T1", min: 800, max: 3200, step: 100, unit: "ms" },
      { key: "t2", label: "T2", min: 0, max: 1200, step: 50, unit: "ms" },
    ],
  };
  const MAX_CANDIDATE_SUB_EVENTS = 96;
  const CURRENT_STEP_EVENT_TYPES = new Set([
    "permission_requested",
    "text_delta",
    "tool_call",
    "tool_result",
    "tool_started",
    "tool_use",
  ]);
  const NORMAL_HANDOFF_TEXT = "部署流程已完成，已进入普通会话。可以继续追问资源、运维或变更需求。";
  const CANDIDATE_SUBSTEP_LABELS = {
    template_generating: "模板生成",
    template_generation: "模板生成",
    template_validating: "模板校验",
    template_validation: "模板校验",
    cost_estimating: "成本估算",
    cost_estimation: "成本估算",
    cost_estimate: "成本估算",
    price_estimating: "价格估算",
    quality_review: "质量复核",
    architecture_review: "架构复核",
    risk_review: "风险复核",
    resource_planning: "资源规划",
    requirement_matching: "需求匹配",
  };
  const CANDIDATE_STEP_IDS = new Set([
    "candidate",
    "candidate_generation",
    "candidate_selection",
    "candidate_summary",
    "cost_estimation",
    "evaluate_candidate",
    "evaluate_candidates",
    "resource_evaluation",
  ]);

  function createSteps() {
    return STEP_ORDER.reduce((steps, stepId) => {
      steps[stepId] = {
        id: stepId,
        label: STEP_LABELS[stepId],
        status: "pending",
        events: [],
      };
      return steps;
    }, {});
  }

  function mergeProgressParams(variant, params) {
    const defaults = DEFAULT_PROGRESS_UI[variant] || {};
    const source = params && typeof params === "object" ? params : {};
    return Object.keys(defaults).reduce((result, key) => {
      const numericValue = Number(source[key]);
      result[key] = Number.isFinite(numericValue) ? numericValue : defaults[key];
      return result;
    }, {});
  }

  function mergeProgressUi(value) {
    const source = value && typeof value === "object" ? value : {};
    const variant = PROGRESS_VARIANT_ORDER.includes(source.variant) ? source.variant : DEFAULT_PROGRESS_UI.variant;
    const rawActiveStepIndex =
      source.activeStepIndex === null || source.activeStepIndex === undefined ? null : Number(source.activeStepIndex);
    return {
      variant,
      activeStepIndex:
        Number.isInteger(rawActiveStepIndex) && rawActiveStepIndex >= 0 && rawActiveStepIndex < STEP_ORDER.length
          ? rawActiveStepIndex
          : null,
      a: mergeProgressParams("a", source.a),
      b: mergeProgressParams("b", source.b),
      d: mergeProgressParams("d", source.d),
    };
  }

  function createInitialState(defaults = {}) {
    const stateDefaults = clonePlainData(defaults && typeof defaults === "object" ? defaults : {});
    return {
      defaults: stateDefaults,
      serverUrl: stateDefaults.serverUrl || "",
      cwd: stateDefaults.cwd || "",
      contextId: "",
      pipelineTaskId: "",
      activeTaskId: "",
      currentStepId: "",
      lastSequence: 0,
      status: "idle",
      pipelineStarted: Boolean(stateDefaults.pipelineStarted),
      normalHandoffReady: false,
      steps: createSteps(),
      candidates: [],
      selectedCandidateIndex: null,
      selectedPendingInputOptionId: stateDefaults.selectedPendingInputOptionId || "",
      pendingInput: null,
      permission: null,
      userMessages: Array.isArray(stateDefaults.userMessages) ? clonePlainData(stateDefaults.userMessages) : [],
      normalTurns: Array.isArray(stateDefaults.normalTurns) ? clonePlainData(stateDefaults.normalTurns) : [],
      pendingNormalUserMessageId: stateDefaults.pendingNormalUserMessageId || "",
      expandedStepDetails: clonePlainData(stateDefaults.expandedStepDetails || {}),
      expandedCandidateSubpipelines: clonePlainData(stateDefaults.expandedCandidateSubpipelines || {}),
      expandedNormalProcesses: clonePlainData(stateDefaults.expandedNormalProcesses || {}),
      progressUi: mergeProgressUi(stateDefaults.progressUi),
      diagnostics: { requests: [], sse: [], snapshots: [] },
    };
  }

  function cloneStep(step) {
    return {
      ...step,
      events: Array.isArray(step.events) ? step.events.map(clonePlainData) : [],
    };
  }

  function cloneCandidate(candidate) {
    return clonePlainData({
      ...candidate,
      costItems: Array.isArray(candidate.costItems) ? candidate.costItems : [],
      subEvents: Array.isArray(candidate.subEvents) ? candidate.subEvents : [],
    });
  }

  function clonePendingInput(pendingInput) {
    if (!pendingInput) {
      return null;
    }
    const nextPendingInput = clonePlainData(pendingInput);
    return {
      ...nextPendingInput,
      prompt: nextPendingInput.prompt || nextPendingInput.question || "",
      options: Array.isArray(nextPendingInput.options) ? nextPendingInput.options : [],
    };
  }

  function cloneDiagnostics(diagnostics) {
    const source = diagnostics || {};
    return clonePlainData({
      requests: Array.isArray(source.requests) ? [...source.requests] : [],
      sse: Array.isArray(source.sse) ? [...source.sse] : [],
      snapshots: Array.isArray(source.snapshots) ? [...source.snapshots] : [],
    });
  }

  function clonePlainData(value) {
    if (Array.isArray(value)) {
      return value.map(clonePlainData);
    }
    if (value && typeof value === "object") {
      return Object.keys(value).reduce((result, key) => {
        result[key] = clonePlainData(value[key]);
        return result;
      }, {});
    }
    return value;
  }

  function cloneState(state) {
    if (!state) {
      return createInitialState();
    }
    const steps = {};
    const defaultSteps = createSteps();
    STEP_ORDER.forEach((stepId) => {
      steps[stepId] = cloneStep(state.steps && state.steps[stepId] ? state.steps[stepId] : defaultSteps[stepId]);
    });
    return {
      ...state,
      defaults: clonePlainData(state.defaults || {}),
      steps,
      candidates: Array.isArray(state.candidates) ? state.candidates.map(cloneCandidate) : [],
      selectedPendingInputOptionId: state.selectedPendingInputOptionId || "",
      pendingInput: clonePendingInput(state.pendingInput),
      permission: clonePlainData(state.permission),
      currentStepId: state.currentStepId || "",
      userMessages: Array.isArray(state.userMessages) ? state.userMessages.map(clonePlainData) : [],
      normalTurns: Array.isArray(state.normalTurns) ? state.normalTurns.map(clonePlainData) : [],
      pendingNormalUserMessageId: state.pendingNormalUserMessageId || "",
      expandedStepDetails: clonePlainData(state.expandedStepDetails || {}),
      expandedCandidateSubpipelines: clonePlainData(state.expandedCandidateSubpipelines || {}),
      expandedNormalProcesses: clonePlainData(state.expandedNormalProcesses || {}),
      pipelineStarted: Boolean(state.pipelineStarted),
      progressUi: mergeProgressUi(state.progressUi),
      diagnostics: cloneDiagnostics(state.diagnostics),
    };
  }

  function pipelineFromMetadata(metadata) {
    if (!metadata || typeof metadata !== "object") {
      return null;
    }
    if (metadata.pipeline) {
      return metadata.pipeline;
    }
    const iacCode = metadata.iac_code || metadata.iacCode || metadata["iac-code"];
    if (iacCode && typeof iacCode === "object") {
      return iacCode.pipeline || iacCode.pipelineEvent || iacCode.pipelineSnapshot || null;
    }
    return null;
  }

  function valueOf(source, ...keys) {
    if (!source || typeof source !== "object") {
      return undefined;
    }
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(source, key)) {
        return source[key];
      }
    }
    return undefined;
  }

  function eventTypeOf(source) {
    return valueOf(source, "eventType", "event_type");
  }

  function taskIdOf(source) {
    return valueOf(source, "deliveryTaskId", "delivery_task_id", "taskId", "task_id");
  }

  function contextIdOf(source) {
    return valueOf(source, "deliveryContextId", "delivery_context_id", "contextId", "context_id");
  }

  function sequenceOf(source) {
    const sequence = valueOf(source, "sequence", "lastSequence", "last_sequence");
    const numericSequence = Number(sequence);
    return Number.isFinite(numericSequence) ? numericSequence : null;
  }

  function pendingInputOf(source) {
    return valueOf(source, "pendingInput", "pending_input", "input");
  }

  function normalHandoffOf(source) {
    return valueOf(source, "normalHandoff", "normal_handoff");
  }

  function targetModeOf(source) {
    return valueOf(source, "targetMode", "target_mode");
  }

  function updateLastSequence(state, sequence) {
    if (typeof sequence === "number") {
      state.lastSequence = Math.max(state.lastSequence || 0, sequence);
    }
  }

  function extractPipelineEnvelope(payload) {
    if (!payload || typeof payload !== "object") {
      return null;
    }
    if (Array.isArray(payload)) {
      for (const item of payload) {
        const envelope = extractPipelineEnvelope(item);
        if (envelope) {
          return envelope;
        }
      }
      return null;
    }

    const metadataPipeline = pipelineFromMetadata(payload.metadata);
    if (metadataPipeline) {
      return metadataPipeline;
    }
    if (payload.iac_code && payload.iac_code.pipeline) {
      return payload.iac_code.pipeline;
    }
    if (payload.iacCode && payload.iacCode.pipeline) {
      return payload.iacCode.pipeline;
    }
    if (payload["iac-code"] && payload["iac-code"].pipeline) {
      return payload["iac-code"].pipeline;
    }
    if (payload.pipeline || payload.pipelineEvent || payload.pipelineSnapshot) {
      return payload.pipeline || payload.pipelineEvent || payload.pipelineSnapshot;
    }
    if (eventTypeOf(payload) || taskIdOf(payload) || contextIdOf(payload) || payload.step) {
      return payload;
    }

    const wrapperKeys = [
      "result",
      "params",
      "task",
      "statusUpdate",
      "status_update",
      "status",
      "message",
      "event",
      "events",
      "snapshot",
    ];
    for (const key of wrapperKeys) {
      if (payload[key] && typeof payload[key] === "object") {
        const envelope = extractPipelineEnvelope(payload[key]);
        if (envelope) {
          return envelope;
        }
      }
    }
    return null;
  }

  function normalizeStatus(status) {
    if (status === "input_required") {
      return "waiting_input";
    }
    return status || "";
  }

  function statusFromEventType(eventType, fallbackStatus) {
    const statuses = {
      step_started: "working",
      step_completed: "completed",
      step_failed: "failed",
      input_required: "waiting_input",
    };
    return statuses[eventType] || normalizeStatus(fallbackStatus);
  }

  function normalizeStepId(step) {
    const rawStepId = typeof step === "string" ? step : step && (step.id || step.name || step.stepId);
    if (!rawStepId) {
      return "";
    }
    const stepId = String(rawStepId);
    if (CANDIDATE_STEP_IDS.has(stepId) || stepId.startsWith("candidate_") || stepId.includes("candidate")) {
      return "evaluate_candidates";
    }
    if (STEP_ORDER.includes(stepId)) {
      return stepId;
    }
    return stepId;
  }

  function normalizeCandidateIndexValue(candidateIndex) {
    if (candidateIndex === null || candidateIndex === undefined || candidateIndex === "") {
      return candidateIndex;
    }
    const numericIndex = Number(candidateIndex);
    return Number.isFinite(numericIndex) ? numericIndex : candidateIndex;
  }

  function candidateFromDisplayItem(item) {
    if (!item || typeof item !== "object") {
      return null;
    }
    const detail = item.detail && typeof item.detail === "object" ? item.detail : item;
    const nestedCandidate =
      item.candidate && typeof item.candidate === "object"
        ? item.candidate
        : detail.candidate && typeof detail.candidate === "object"
          ? detail.candidate
          : {};
    const cost =
      item.cost && typeof item.cost === "object"
        ? item.cost
        : detail.cost && typeof detail.cost === "object"
          ? detail.cost
          : {};
    const conclusions =
      item.conclusions && typeof item.conclusions === "object"
        ? item.conclusions
        : detail.conclusions && typeof detail.conclusions === "object"
          ? detail.conclusions
          : {};
    const templateConclusion = conclusions.template && typeof conclusions.template === "object" ? conclusions.template : {};
    const costConclusion = conclusions.cost && typeof conclusions.cost === "object" ? conclusions.cost : {};
    const primitiveCost = (value) => (value && typeof value === "object" ? undefined : value);
    const candidateIndex =
      item.candidateIndex ??
      item.candidate_index ??
      item.optionIndex ??
      item.option_index ??
      item.index ??
      item.id ??
      (item.candidate && item.candidate.index) ??
      detail.candidateIndex ??
      detail.candidate_index ??
      detail.optionIndex ??
      detail.option_index ??
      detail.index ??
      detail.id ??
      null;
    return {
      name:
        item.name ||
        item.candidateName ||
        item.candidate_name ||
        detail.candidateName ||
        detail.candidate_name ||
        nestedCandidate.candidateName ||
        nestedCandidate.candidate_name ||
        detail.name ||
        nestedCandidate.name ||
        item.title ||
        detail.title ||
        nestedCandidate.title ||
        item.label ||
        detail.label ||
        nestedCandidate.label ||
        item.template ||
        detail.template ||
        "",
      candidateIndex: normalizeCandidateIndexValue(candidateIndex),
      summary:
        item.summary ||
        item.firstVersionDescription ||
        item.first_version_description ||
        item.planDescription ||
        item.plan_description ||
        item.pros ||
        item.topology ||
        detail.summary ||
        detail.firstVersionDescription ||
        detail.first_version_description ||
        detail.planDescription ||
        detail.plan_description ||
        detail.pros ||
        detail.topology ||
        templateConclusion.summary ||
        templateConclusion.description ||
        nestedCandidate.summary ||
        nestedCandidate.firstVersionDescription ||
        nestedCandidate.first_version_description ||
        item.description ||
        detail.description ||
        nestedCandidate.description ||
        nestedCandidate.topology ||
        nestedCandidate.pros ||
        "",
      template: item.template || detail.template || "",
      totalMonthlyCost:
        item.totalMonthlyCost ??
        item.total_monthly_cost ??
        item.monthlyCost ??
        item.monthly_cost ??
        item.monthlyEstimate ??
        item.monthly_estimate ??
        item.roughMonthlyEstimate ??
        item.rough_monthly_estimate ??
        item.estimatedMonthlyCost ??
        item.estimated_monthly_cost ??
        primitiveCost(item.cost) ??
        item.price ??
        detail.totalMonthlyCost ??
        detail.total_monthly_cost ??
        detail.monthlyCost ??
        detail.monthly_cost ??
        detail.monthlyEstimate ??
        detail.monthly_estimate ??
        detail.roughMonthlyEstimate ??
        detail.rough_monthly_estimate ??
        detail.estimatedMonthlyCost ??
        detail.estimated_monthly_cost ??
        primitiveCost(detail.cost) ??
        detail.price ??
        cost.totalMonthlyCost ??
        cost.total_monthly_cost ??
        cost.monthlyCost ??
        cost.monthly_cost ??
        cost.monthlyEstimate ??
        cost.monthly_estimate ??
        costConclusion.totalMonthlyCost ??
        costConclusion.total_monthly_cost ??
        costConclusion.monthlyEstimate ??
        costConclusion.monthly_estimate ??
        nestedCandidate.totalMonthlyCost ??
        nestedCandidate.total_monthly_cost ??
        nestedCandidate.monthlyEstimate ??
        nestedCandidate.monthly_estimate ??
        "",
      outputPath:
        item.outputPath ||
        item.output_path ||
        detail.outputPath ||
        detail.output_path ||
        templateConclusion.outputPath ||
        templateConclusion.output_path ||
        templateConclusion.filePath ||
        templateConclusion.file_path ||
        nestedCandidate.outputPath ||
        nestedCandidate.output_path ||
        "",
      costItems: Array.isArray(item.costItems)
        ? clonePlainData(item.costItems)
        : Array.isArray(detail.costItems)
          ? clonePlainData(detail.costItems)
          : Array.isArray(cost.costItems)
            ? clonePlainData(cost.costItems)
            : Array.isArray(cost.items)
              ? clonePlainData(cost.items)
              : Array.isArray(cost.resources)
                ? clonePlainData(cost.resources)
                : Array.isArray(costConclusion.costItems)
                  ? clonePlainData(costConclusion.costItems)
                  : Array.isArray(costConclusion.items)
                    ? clonePlainData(costConclusion.items)
                    : Array.isArray(costConclusion.resources)
                      ? clonePlainData(costConclusion.resources)
                      : [],
    };
  }

  function candidateIndexFromSource(source) {
    if (!source || typeof source !== "object") {
      return null;
    }
    const data = source.data && typeof source.data === "object" ? source.data : {};
    const candidate = source.candidate && typeof source.candidate === "object" ? source.candidate : {};
    const rawIndex =
      source.candidateIndex ??
      source.candidate_index ??
      source.optionIndex ??
      source.option_index ??
      candidate.index ??
      candidate.id ??
      candidate.candidateIndex ??
      candidate.candidate_index ??
      data.candidateIndex ??
      data.candidate_index ??
      data.optionIndex ??
      data.option_index ??
      null;
    const normalizedIndex = normalizeCandidateIndexValue(rawIndex);
    return normalizedIndex === "" || normalizedIndex === null || normalizedIndex === undefined ? null : normalizedIndex;
  }

  function candidateSelectionInputKind(source) {
    if (!source || typeof source !== "object") {
      return "";
    }
    return String(source.kind || source.inputKind || source.input_kind || source.type || "");
  }

  function hasCandidateSelectionOptions(source) {
    const kind = candidateSelectionInputKind(source);
    return (kind === "candidate_selection" || kind === "candidate_select") && Array.isArray(source.options);
  }

  function isCandidateSubPipelineEvent(envelope, stepId) {
    const eventType = eventTypeOf(envelope || {});
    const candidateIndex = candidateIndexFromSource(envelope);
    if (candidateIndex === null || candidateIndex === undefined) {
      return false;
    }
    if (String(eventType || "").startsWith("candidate_step")) {
      return true;
    }
    if (eventType === "candidate_started" || eventType === "candidate_completed" || eventType === "candidate_failed") {
      return true;
    }
    if (envelope.candidateStep || envelope.candidate_step) {
      return true;
    }
    return (
      stepId === "evaluate_candidates" &&
      ["text_delta", "tool_result", "tool_use", "tool_call", "tool_started", "permission_requested"].includes(eventType)
    );
  }

  function appendCandidateSubEventInPlace(state, envelope) {
    const candidateIndex = candidateIndexFromSource(envelope);
    if (candidateIndex === null || candidateIndex === undefined) {
      return state;
    }
    upsertCandidateInPlace(state, {
      candidateIndex,
      name:
        envelope &&
        envelope.candidate &&
        typeof envelope.candidate === "object" &&
        (envelope.candidate.name || envelope.candidate.title || envelope.candidate.label),
    });
    const targetIndex = state.candidates.findIndex(
      (candidate) => normalizeCandidateIndexValue(candidate.candidateIndex) === candidateIndex
    );
    if (targetIndex < 0) {
      return state;
    }
    const target = cloneCandidate(state.candidates[targetIndex]);
    target.subEvents = Array.isArray(target.subEvents) ? target.subEvents : [];
    target.subEvents.push(clonePlainData(envelope));
    target.subEvents = target.subEvents.slice(-MAX_CANDIDATE_SUB_EVENTS);
    state.candidates[targetIndex] = target;
    return state;
  }

  function candidateCollectionsFromSource(source) {
    if (!source || typeof source !== "object") {
      return [];
    }
    const collections = [];
    const collectFromObject = (target, options = {}) => {
      if (!target || typeof target !== "object") {
        return;
      }
      collections.push(
        target.candidateDetails,
        target.candidate_details,
        target.candidates,
        target.draftCandidates,
        target.draft_candidates,
        target.planCandidates,
        target.plan_candidates,
        target.candidateOptions,
        target.candidate_options,
        target.candidateSummaries,
        target.candidate_summaries,
        target.plans,
        target.proposals
      );
      if (options.includeGenericOptions) {
        collections.push(target.options);
      }
    };
    const display = source.display && typeof source.display === "object" ? source.display : null;
    if (display) {
      collectFromObject(display, { includeGenericOptions: true });
    }
    collectFromObject(source);
    if (hasCandidateSelectionOptions(source)) {
      collections.push(source.options);
    }
    const pendingInput = pendingInputOf(source);
    if (pendingInput && typeof pendingInput === "object" && hasCandidateSelectionOptions(pendingInput)) {
      collections.push(pendingInput.options);
    }
    const conclusion = source.conclusion && typeof source.conclusion === "object" ? source.conclusion : null;
    if (conclusion) {
      collectFromObject(conclusion, { includeGenericOptions: true });
    }
    const data = source.data && typeof source.data === "object" ? source.data : null;
    if (data && data !== source) {
      collections.push(...candidateCollectionsFromSource(data));
    }
    return collections.filter(Array.isArray);
  }

  function numericConclusionItems(conclusion) {
    if (!conclusion || typeof conclusion !== "object" || Array.isArray(conclusion)) {
      return [];
    }
    return Object.keys(conclusion)
      .filter((key) => /^\d+$/.test(key) && conclusion[key] && typeof conclusion[key] === "object")
      .map((key) => ({
        index: Number(key),
        candidateIndex: Number(key),
        ...conclusion[key],
      }));
  }

  function upsertCandidatesFromSource(state, source) {
    candidateCollectionsFromSource(source).forEach((collection) => {
      collection.forEach((item) => {
        upsertCandidateInPlace(state, candidateFromDisplayItem(item));
      });
    });
    const upsertNumericConclusionItems = (current) => {
      const conclusion = current && current.conclusion && typeof current.conclusion === "object" ? current.conclusion : null;
      numericConclusionItems(conclusion).forEach((item) => {
        upsertCandidateInPlace(state, candidateFromDisplayItem(item));
      });
      const data = current && current.data && typeof current.data === "object" ? current.data : null;
      if (data && data !== current) {
        upsertNumericConclusionItems(data);
      }
    };
    upsertNumericConclusionItems(source);
    return state;
  }

  function candidateFromEnvelope(envelope) {
    if (!envelope || typeof envelope !== "object") {
      return null;
    }
    const data = envelope.data && typeof envelope.data === "object" ? envelope.data : {};
    const conclusion = data.conclusion && typeof data.conclusion === "object" ? data.conclusion : {};
    const detail =
      data.detail && typeof data.detail === "object"
        ? data.detail
        : data.candidate_detail && typeof data.candidate_detail === "object"
          ? data.candidate_detail
          : {};
    const eventCandidate = envelope.candidate && typeof envelope.candidate === "object" ? envelope.candidate : {};
    const dataCandidate = data.candidate && typeof data.candidate === "object" ? data.candidate : {};
    const conclusionCandidate =
      conclusion.candidate && typeof conclusion.candidate === "object" ? conclusion.candidate : {};
    const conclusions = data.conclusions && typeof data.conclusions === "object" ? data.conclusions : {};
    const templateConclusion = conclusions.template && typeof conclusions.template === "object" ? conclusions.template : {};
    const costConclusion = conclusions.cost && typeof conclusions.cost === "object" ? conclusions.cost : {};
    const candidateIndex = candidateIndexFromSource(envelope);
    return candidateFromDisplayItem({
      ...data,
      ...conclusion,
      ...templateConclusion,
      detail: Object.keys(detail).length ? detail : { ...conclusion, ...templateConclusion },
      cost: Object.keys(costConclusion).length ? costConclusion : data.cost,
      candidate: {
        ...eventCandidate,
        ...dataCandidate,
        ...conclusionCandidate,
      },
      candidateIndex,
    });
  }

  function hasCandidateValue(value) {
    if (Array.isArray(value)) {
      return value.length > 0;
    }
    return value !== "" && value !== null && value !== undefined;
  }

  function mergeCandidate(existing, candidate) {
    const result = cloneCandidate(existing || {});
    Object.keys(candidate || {}).forEach((key) => {
      const value = candidate[key];
      if (hasCandidateValue(value)) {
        result[key] = clonePlainData(value);
      } else if (!Object.prototype.hasOwnProperty.call(result, key)) {
        result[key] = clonePlainData(value);
      }
    });
    return cloneCandidate(result);
  }

  function upsertCandidateInPlace(state, candidate) {
    if (!candidate) {
      return state;
    }
    const nextCandidate = cloneCandidate(candidate);
    nextCandidate.candidateIndex = normalizeCandidateIndexValue(nextCandidate.candidateIndex);
    const hasNextIndex = nextCandidate.candidateIndex !== null && nextCandidate.candidateIndex !== undefined;
    const index = state.candidates.findIndex((existing) => {
      if (hasNextIndex && normalizeCandidateIndexValue(existing.candidateIndex) === nextCandidate.candidateIndex) {
        return true;
      }
      if (existing.name && nextCandidate.name && existing.name === nextCandidate.name) {
        return true;
      }
      return false;
    });
    if (index >= 0) {
      state.candidates[index] = mergeCandidate(state.candidates[index], nextCandidate);
    } else {
      state.candidates.push(nextCandidate);
    }
    return state;
  }

  function upsertCandidate(state, candidate) {
    const nextState = cloneState(state);
    return upsertCandidateInPlace(nextState, candidate);
  }

  function pendingInputFromSnapshot(snapshot) {
    const pendingInput = pendingInputOf(snapshot);
    if (!pendingInput) {
      return null;
    }
    return pendingInputFromInput(pendingInput);
  }

  function pendingInputFromInput(input) {
    if (!input || typeof input !== "object") {
      return null;
    }
    const pendingInput = clonePlainData(input);
    return {
      ...pendingInput,
      prompt: pendingInput.prompt || pendingInput.question || "",
      options: Array.isArray(pendingInput.options) ? pendingInput.options : [],
    };
  }

  function applySnapshot(state, snapshot) {
    if (!snapshot || typeof snapshot !== "object") {
      return state;
    }
    const taskId = taskIdOf(snapshot);
    if (taskId) {
      state.pipelineTaskId = taskId;
    }
    const contextId = contextIdOf(snapshot);
    if (contextId) {
      state.contextId = contextId;
    }
    updateLastSequence(state, sequenceOf(snapshot));
    if (snapshot.status) {
      state.status = normalizeStatus(snapshot.status);
      if (state.status && state.status !== "idle") {
        state.pipelineStarted = true;
      }
    }

    if (Array.isArray(snapshot.steps)) {
      snapshot.steps.forEach((step) => {
        const stepId = normalizeStepId(step);
        if (stepId && state.steps[stepId]) {
          const status = normalizeStatus(step.status) || state.steps[stepId].status;
          state.steps[stepId].status = status;
          if (status && status !== "pending") {
            state.pipelineStarted = true;
          }
          if (status === "working" || status === "waiting_input") {
            state.currentStepId = stepId;
          }
        }
      });
    }

    upsertCandidatesFromSource(state, snapshot);

    const pendingInput = pendingInputFromSnapshot(snapshot);
    if (
      Object.prototype.hasOwnProperty.call(snapshot, "pendingInput") ||
      Object.prototype.hasOwnProperty.call(snapshot, "pending_input")
    ) {
      state.pendingInput = pendingInputFromInput(pendingInputOf(snapshot));
    } else if (pendingInput) {
      state.pendingInput = pendingInput;
    }

    const normalHandoff = normalHandoffOf(snapshot);
    if (
      normalHandoff &&
      typeof normalHandoff === "object" &&
      normalHandoff.action === "switch_to_normal" &&
      targetModeOf(normalHandoff) === "normal"
    ) {
      state.normalHandoffReady = true;
      state.activeTaskId = "";
    }
    return state;
  }

  function currentStepIdFromState(state) {
    const isActive = (stepId) => {
      const status = stepStatusClass(normalizeStatus(state && state.steps && state.steps[stepId] && state.steps[stepId].status));
      return status === "working" || status === "waiting_input";
    };
    if (state && state.currentStepId && state.steps && state.steps[state.currentStepId] && isActive(state.currentStepId)) {
      return state.currentStepId;
    }
    const activeStepId = STEP_ORDER.find((stepId) => isActive(stepId));
    return activeStepId || "";
  }

  function inferredStepIdForEvent(state, envelope, explicitStepId) {
    if (explicitStepId) {
      return explicitStepId;
    }
    if (!CURRENT_STEP_EVENT_TYPES.has(eventTypeOf(envelope))) {
      return "";
    }
    return currentStepIdFromState(state);
  }

  function applyPipelineEnvelope(state, envelope) {
    if (!envelope) {
      return state;
    }
    const eventType = eventTypeOf(envelope);
    const taskId = taskIdOf(envelope);
    if (taskId) {
      state.pipelineTaskId = taskId;
    }
    const contextId = contextIdOf(envelope);
    if (contextId) {
      state.contextId = contextId;
    }
    updateLastSequence(state, sequenceOf(envelope));
    if (envelope.status) {
      state.status = normalizeStatus(envelope.status);
    }

    const explicitStepId = normalizeStepId(envelope.step);
    const stepId = inferredStepIdForEvent(state, envelope, explicitStepId);
    if (eventType === "pipeline_started" || stepId) {
      state.pipelineStarted = true;
    }
    if (stepId && state.steps[stepId]) {
      state.currentStepId = stepId;
      state.steps[stepId].status =
        statusFromEventType(eventType, (envelope.step && envelope.step.status) || envelope.status) ||
        state.steps[stepId].status;
      state.steps[stepId].events.push(clonePlainData(envelope));
      if (eventType === "step_completed" && state.expandedStepDetails) {
        state.expandedStepDetails[stepId] = false;
      }
    }
    if (isCandidateSubPipelineEvent(envelope, stepId)) {
      appendCandidateSubEventInPlace(state, envelope);
    }

    const data = envelope.data || {};
    if (eventType === "candidate_completed" || eventType === "candidate_failed") {
      upsertCandidateInPlace(state, candidateFromEnvelope(envelope));
      const candidateIndex = candidateIndexFromSource(envelope);
      if (candidateIndex !== null && candidateIndex !== undefined) {
        state.expandedCandidateSubpipelines = state.expandedCandidateSubpipelines || {};
        state.expandedCandidateSubpipelines[String(candidateIndex)] = false;
      }
    }
    if (eventType === "candidate_detail_shown") {
      upsertCandidateInPlace(
        state,
        candidateFromDisplayItem({
          ...data,
          candidate: envelope.candidate || data.candidate,
          step: envelope.step || data.step,
        })
      );
    }
    upsertCandidatesFromSource(state, envelope);
    if (eventType === "input_required") {
      state.pendingInput = pendingInputFromInput(pendingInputOf(envelope) || data);
    }
    if (eventType === "input_received") {
      state.pendingInput = null;
    }
    if (
      eventType === "pipeline_handoff_ready" ||
      (data.action === "switch_to_normal" && targetModeOf(data) === "normal")
    ) {
      state.normalHandoffReady = true;
      state.activeTaskId = "";
      if (envelope.status) {
        state.status = normalizeStatus(envelope.status);
      }
    }
    return state;
  }

  function isSnapshotLike(payload) {
    if (!payload || typeof payload !== "object") {
      return false;
    }
    if (eventTypeOf(payload)) {
      return false;
    }
    return Boolean(
      payload.display ||
        Object.prototype.hasOwnProperty.call(payload, "pendingInput") ||
        Object.prototype.hasOwnProperty.call(payload, "pending_input") ||
        normalHandoffOf(payload) ||
        taskIdOf(payload) ||
        contextIdOf(payload) ||
        sequenceOf(payload) !== null ||
        Array.isArray(payload.steps)
    );
  }

  function reducePipelinePayload(state, payload) {
    const nextState = cloneState(state);
    const hasEvents = payload && Array.isArray(payload.events);
    applyPipelineEnvelope(nextState, hasEvents ? null : extractPipelineEnvelope(payload));
    if (payload && payload.snapshot) {
      applySnapshot(nextState, payload.snapshot);
    } else if (isSnapshotLike(payload)) {
      applySnapshot(nextState, payload);
    }
    if (payload && Array.isArray(payload.events)) {
      payload.events.forEach((event) => {
        applyPipelineEnvelope(nextState, extractPipelineEnvelope(event));
      });
    }
    applyNormalChatPayload(nextState, payload);
    return nextState;
  }

  function a2aSource(payload) {
    if (!payload || typeof payload !== "object") {
      return null;
    }
    if (Array.isArray(payload)) {
      for (const item of payload) {
        const source = a2aSource(item);
        if (source) {
          return source;
        }
      }
      return null;
    }
    if (payload.status && typeof payload.status === "object") {
      return payload;
    }
    if (payload.metadata && typeof payload.metadata === "object") {
      return payload;
    }
    for (const key of ["result", "params", "event", "task"]) {
      if (payload[key] && typeof payload[key] === "object") {
        const source = a2aSource(payload[key]);
        if (source) {
          return source;
        }
      }
    }
    return null;
  }

  function a2aTaskId(source) {
    return (
      taskIdOf(source || {}) ||
      valueOf(source || {}, "id") ||
      (source && source.task && typeof source.task === "object" && (taskIdOf(source.task) || source.task.id)) ||
      ""
    );
  }

  function normalizeA2aState(value) {
    if (!value) {
      return "";
    }
    const normalized = String(value)
      .trim()
      .toLowerCase()
      .replace(/^task_state_/, "")
      .replace(/-/g, "_");
    if (normalized === "input_required") {
      return "completed";
    }
    if (normalized === "completed" || normalized === "failed" || normalized === "canceled" || normalized === "working") {
      return normalized;
    }
    return normalized;
  }

  function partText(part) {
    if (typeof part === "string") {
      return part;
    }
    if (!part || typeof part !== "object") {
      return "";
    }
    if (typeof part.text === "string") {
      return part.text;
    }
    if (part.root && typeof part.root === "object") {
      return partText(part.root);
    }
    if (part.data && typeof part.data === "object" && typeof part.data.text === "string") {
      return part.data.text;
    }
    return "";
  }

  function contentBlockText(block) {
    if (typeof block === "string") {
      return block;
    }
    if (!block || typeof block !== "object") {
      return "";
    }
    const type = String(block.type || block.kind || "").toLowerCase();
    if (type && type !== "text" && type !== "output_text") {
      return "";
    }
    if (typeof block.text === "string") {
      return block.text;
    }
    if (typeof block.content === "string") {
      return block.content;
    }
    return partText(block);
  }

  function messageText(message) {
    if (typeof message === "string") {
      return message;
    }
    if (!message || typeof message !== "object") {
      return "";
    }
    if (typeof message.text === "string") {
      return message.text;
    }
    if (Array.isArray(message.content)) {
      return message.content.map(contentBlockText).join("");
    }
    const parts = Array.isArray(message.parts) ? message.parts : [];
    return parts.map(partText).join("");
  }

  function agentHistoryEntryText(source) {
    const history = Array.isArray(source && source.history)
      ? source.history
      : Array.isArray(source && source.task && source.task.history)
        ? source.task.history
        : [];
    for (let index = history.length - 1; index >= 0; index -= 1) {
      const entry = history[index];
      const role = String((entry && entry.role) || "")
        .toLowerCase()
        .replace(/^role_/, "");
      if (!["agent", "assistant"].includes(role)) {
        continue;
      }
      const text = messageText(entry);
      if (text) {
        return text;
      }
    }
    return "";
  }

  function normalAnswerFromSource(source, status) {
    const liveText = messageText((status && status.message) || (source && source.message));
    if (liveText) {
      return { text: liveText, replace: false };
    }
    const historyText = agentHistoryEntryText(source);
    return historyText ? { text: historyText, replace: true } : { text: "", replace: false };
  }

  function mergeNormalAnswer(existing, next, replace) {
    if (!next) {
      return existing || "";
    }
    if (!replace) {
      return `${existing || ""}${next}`;
    }
    if (!existing) {
      return next;
    }
    if (next.includes(existing) || existing.includes(next)) {
      return next.length >= existing.length ? next : existing;
    }
    return `${existing}${next}`;
  }

  function iacMetadata(source) {
    const metadata = source && source.metadata && typeof source.metadata === "object" ? source.metadata : {};
    const statusMetadata =
      source && source.status && source.status.metadata && typeof source.status.metadata === "object"
        ? source.status.metadata
        : {};
    return (
      metadata.iac_code ||
      metadata.iacCode ||
      metadata["iac-code"] ||
      statusMetadata.iac_code ||
      statusMetadata.iacCode ||
      statusMetadata["iac-code"] ||
      null
    );
  }

  function compactValueText(value) {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
    if (typeof value === "object") {
      return (
        value.content ||
        value.text ||
        value.summary ||
        value.safeSummary ||
        value.message ||
        value.error ||
        ""
      );
    }
    return "";
  }

  function normalToolText(tool) {
    if (!tool || typeof tool !== "object") {
      return "";
    }
    const statusMap = {
      started: "开始",
      input_delta: "输入中",
      input_complete: "输入完成",
      completed: "完成",
      failed: "失败",
    };
    const name = tool.name || tool.toolName || "工具";
    const status = statusMap[tool.status] || tool.status || "";
    const result = compactValueText(tool.result || tool.artifact || tool.input || tool.partialJson);
    return [name, status, result].filter(Boolean).join(" ");
  }

  function normalEventsFromMetadata(metadata) {
    if (!metadata || typeof metadata !== "object") {
      return [];
    }
    const events = [];
    if (metadata.thinking && typeof metadata.thinking === "object") {
      const text = compactValueText(metadata.thinking.text || metadata.thinking);
      if (text) {
        events.push({ kind: "thinking", label: "思考", text });
      }
    }
    if (metadata.tool && typeof metadata.tool === "object") {
      const text = normalToolText(metadata.tool);
      if (text) {
        events.push({ kind: "tool", label: "工具", text });
      }
    }
    if (metadata.permission && typeof metadata.permission === "object") {
      const text = metadata.permission.toolName || metadata.permission.tool_name || "权限确认";
      events.push({ kind: "permission", label: "权限", text });
    }
    if (metadata.error && typeof metadata.error === "object") {
      const text = compactValueText(metadata.error.message || metadata.error.error || metadata.error);
      if (text) {
        events.push({ kind: "error", label: "异常", text });
      }
    }
    return events;
  }

  function lastNormalUserMessageId(state) {
    const messages = Array.isArray(state && state.userMessages) ? state.userMessages : [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      const placement = userMessagePlacement(message);
      if (placement.position === "after_normal_handoff") {
        return userMessageKey(message, index);
      }
    }
    return "";
  }

  function normalTurnForEvent(state, taskId, shouldCreate) {
    state.normalTurns = Array.isArray(state.normalTurns) ? state.normalTurns : [];
    const id = taskId || `normal-turn-${state.normalTurns.length + 1}`;
    let index = state.normalTurns.findIndex((turn) => turn && (turn.taskId === taskId || turn.id === id));
    if (index < 0) {
      if (!shouldCreate) {
        return null;
      }
      const afterUserMessageId = state.pendingNormalUserMessageId || lastNormalUserMessageId(state);
      state.normalTurns.push({
        id,
        taskId,
        afterUserMessageId,
        status: "working",
        answer: "",
        events: [],
      });
      state.pendingNormalUserMessageId = "";
      index = state.normalTurns.length - 1;
    }
    state.normalTurns[index].events = Array.isArray(state.normalTurns[index].events) ? state.normalTurns[index].events : [];
    return state.normalTurns[index];
  }

  function applyNormalChatPayload(state, payload) {
    if (!state || !state.normalHandoffReady) {
      return state;
    }
    const pipelineEnvelope = extractPipelineEnvelope(payload);
    if (pipelineEnvelope && eventTypeOf(pipelineEnvelope)) {
      return state;
    }
    const source = a2aSource(payload);
    if (!source) {
      return state;
    }
    const status = source.status && typeof source.status === "object" ? source.status : {};
    const stateValue = normalizeA2aState(status.state || source.state || source.status);
    const answer = normalAnswerFromSource(source, status);
    const answerText = answer.text;
    const events = normalEventsFromMetadata(iacMetadata(source));
    const taskId = a2aTaskId(source);
    const shouldCreate = Boolean(answerText || events.length || stateValue === "working");
    const turn = normalTurnForEvent(state, taskId, shouldCreate);
    if (!turn) {
      return state;
    }
    if (taskId) {
      turn.taskId = taskId;
    }
    if (answerText) {
      turn.answer = mergeNormalAnswer(turn.answer, answerText, answer.replace);
    }
    events.forEach((event) => {
      turn.events.push(clonePlainData(event));
    });
    turn.events = turn.events.slice(-80);
    if (stateValue === "working") {
      turn.status = "working";
    } else if (stateValue === "failed" || stateValue === "canceled") {
      turn.status = stateValue;
    } else if (stateValue) {
      turn.status = "completed";
    }
    return state;
  }

  function buildStreamPayload(state, prompt) {
    const source = state && typeof state === "object" ? state : {};
    return {
      serverUrl: source.serverUrl || "",
      cwd: source.cwd || "",
      contextId: source.contextId || "",
      taskId: source.normalHandoffReady ? "" : source.activeTaskId || source.pipelineTaskId || "",
      prompt: prompt || "",
    };
  }

  function selectCandidate(state, candidateIndex) {
    const nextState = state && typeof state === "object" ? state : createInitialState();
    const numericIndex = Number(candidateIndex);
    nextState.selectedCandidateIndex = Number.isFinite(numericIndex) ? numericIndex : null;
    return nextState;
  }

  function promptForSelectedCandidate(state) {
    if (!state || state.selectedCandidateIndex === null || state.selectedCandidateIndex === undefined) {
      return "";
    }
    const numericIndex = Number(state.selectedCandidateIndex);
    if (!Number.isFinite(numericIndex)) {
      return "";
    }
    return `选择方案${numericIndex}`;
  }

  window.SellingConsoleReducers = {
    STEP_ORDER,
    STEP_LABELS,
    createInitialState,
    extractPipelineEnvelope,
    normalizeStepId,
    upsertCandidate,
    reducePipelinePayload,
    candidateFromDisplayItem,
    pendingInputFromSnapshot,
    buildStreamPayload,
    selectCandidate,
    promptForSelectedCandidate,
  };

  const STEP_DESCRIPTIONS = {
    intent_parsing: "识别业务目标、地域、预算与部署约束。",
    architecture_planning: "拆解网络、计算、存储与安全资源拓扑。",
    evaluate_candidates: "比较规格、可用区、成本与运维复杂度。",
    confirm_and_select: "确认推荐方案并准备转入标准部署流程。",
    deploying: "复核资源清单、交付方式与后续部署动作。",
  };
  const CONCLUSION_FIELD_LABELS = {
    architecture: "架构",
    budget: "预算",
    intent: "需求",
    isInfraIntent: "基础设施需求",
    is_infra_intent: "基础设施需求",
    objective: "目标",
    plan: "方案",
    reason: "原因",
    recommendation: "推荐",
    region: "地域",
    scenario: "场景",
    selectedOption: "已选方案",
    selectedValue: "已选项",
    summary: "总结",
  };
  const STATUS_LABELS = {
    idle: "等待输入",
    pending: "未开始",
    working: "进行中",
    completed: "已完成",
    waiting_input: "等待输入",
    failed: "失败",
    error: "失败",
  };
  const PROGRESS_STATUS_LABELS = {
    pending: "待开始",
    working: "思考中",
    completed: "完成",
    waiting_input: "待确认",
    failed: "失败",
    error: "失败",
  };
  const STEP_DETAIL_STATUS_LABELS = {
    working: "思考中",
    completed: "思考完成",
    waiting_input: "等待确认",
    failed: "执行失败",
    error: "执行失败",
  };
  const STEP_STATUS_CLASSES = new Set(["pending", "working", "completed", "waiting_input", "failed", "error"]);

  const controller = {
    state: null,
    bound: false,
    progressAnimationFrame: null,
    progressAnimationToken: 0,
    progressRunTimer: 0,
    progressWaitTimer: 0,
  };

  function hasDocument() {
    return typeof document !== "undefined" && document !== null;
  }

  function canCreateElements() {
    return hasDocument() && typeof document.createElement === "function";
  }

  function query(selector) {
    if (!hasDocument() || typeof document.querySelector !== "function") {
      return null;
    }
    return document.querySelector(selector);
  }

  function byId(id) {
    if (!hasDocument()) {
      return null;
    }
    if (typeof document.getElementById === "function") {
      return document.getElementById(id);
    }
    return query(`#${id}`);
  }

  function clearElement(element) {
    if (!element) {
      return;
    }
    if (typeof element.replaceChildren === "function") {
      element.replaceChildren();
      return;
    }
    while (element.firstChild && typeof element.removeChild === "function") {
      element.removeChild(element.firstChild);
    }
    if (!element.firstChild) {
      element.textContent = "";
    }
  }

  function appendChild(parent, child) {
    if (parent && child && typeof parent.appendChild === "function") {
      parent.appendChild(child);
    }
  }

  function createElement(tagName, className, text) {
    if (!canCreateElements()) {
      return null;
    }
    const svgTags = new Set(["svg", "path"]);
    const element =
      svgTags.has(tagName) && typeof document.createElementNS === "function"
        ? document.createElementNS("http://www.w3.org/2000/svg", tagName)
        : document.createElement(tagName);
    if (className) {
      if (typeof element.setAttribute === "function") {
        element.setAttribute("class", className);
      } else {
        element.className = className;
      }
    }
    if (text !== undefined && text !== null) {
      element.textContent = String(text);
    }
    return element;
  }

  function addClassName(element, className) {
    if (!element || !className) {
      return element;
    }
    const current =
      (typeof element.getAttribute === "function" && element.getAttribute("class")) || element.className || "";
    const classes = new Set(String(current || "").split(/\s+/).filter(Boolean));
    String(className || "")
      .split(/\s+/)
      .filter(Boolean)
      .forEach((item) => classes.add(item));
    const nextClassName = Array.from(classes).join(" ");
    if (typeof element.setAttribute === "function") {
      element.setAttribute("class", nextClassName);
    } else {
      element.className = nextClassName;
    }
    return element;
  }

  function markMarkdownNode(element, kind) {
    if (element && kind) {
      element.setAttribute("data-markdown-node", kind);
    }
    return element;
  }

  function safeMarkdownUrl(value) {
    const url = String(value || "").trim();
    if (/^(https?:|mailto:)/i.test(url)) {
      return url;
    }
    return "";
  }

  function appendInlineMarkdown(parent, text) {
    if (!parent) {
      return;
    }
    const source = String(text || "");
    const tokenPattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
    let cursor = 0;
    source.replace(tokenPattern, (match, _token, offset) => {
      if (offset > cursor) {
        appendChild(parent, createElement("span", "", source.slice(cursor, offset)));
      }
      if (match.startsWith("**")) {
        appendChild(parent, markMarkdownNode(createElement("strong", "", match.slice(2, -2)), "strong"));
      } else if (match.startsWith("`")) {
        appendChild(parent, markMarkdownNode(createElement("code", "", match.slice(1, -1)), "code"));
      } else {
        const linkMatch = match.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const link = createElement("a", "", linkMatch ? linkMatch[1] : match);
        const href = linkMatch ? safeMarkdownUrl(linkMatch[2]) : "";
        if (link && href) {
          link.setAttribute("href", href);
          link.setAttribute("target", "_blank");
          link.setAttribute("rel", "noreferrer");
        }
        appendChild(parent, markMarkdownNode(link, "a"));
      }
      cursor = offset + match.length;
      return match;
    });
    if (cursor < source.length) {
      appendChild(parent, createElement("span", "", source.slice(cursor)));
    }
  }

  function markdownLines(value) {
    return String(value || "")
      .replace(/\r\n?/g, "\n")
      .replace(/([^\n])\s+(\d+[.)]\s+)/g, "$1\n$2")
      .split("\n");
  }

  function renderMarkdownText(value, className) {
    const container = createElement("div", className || "markdown-text");
    if (container) {
      container.setAttribute("data-markdown-rendered", "true");
    }
    const lines = markdownLines(value);
    let paragraph = [];
    const flushParagraph = () => {
      if (paragraph.length === 0) {
        return;
      }
      const node = createElement("p");
      appendInlineMarkdown(node, paragraph.join(" ").trim());
      appendChild(container, node);
      paragraph = [];
    };
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const trimmed = line.trim();
      if (!trimmed) {
        flushParagraph();
        continue;
      }
      if (/^[-*]\s+/.test(trimmed)) {
        flushParagraph();
        const list = createElement("ul");
        while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
          const item = markMarkdownNode(createElement("li"), "li");
          appendInlineMarkdown(item, lines[index].trim().replace(/^[-*]\s+/, ""));
          appendChild(list, item);
          index += 1;
        }
        index -= 1;
        appendChild(container, list);
        continue;
      }
      if (/^\d+[.)]\s+/.test(trimmed)) {
        flushParagraph();
        const list = markMarkdownNode(createElement("ol"), "ol");
        while (index < lines.length && /^\d+[.)]\s+/.test(lines[index].trim())) {
          const item = markMarkdownNode(createElement("li"), "li");
          appendInlineMarkdown(item, lines[index].trim().replace(/^\d+[.)]\s+/, ""));
          appendChild(list, item);
          index += 1;
        }
        index -= 1;
        appendChild(container, list);
        continue;
      }
      paragraph.push(trimmed);
    }
    flushParagraph();
    if (container && container.children.length === 0) {
      appendChild(container, createElement("p", "", ""));
    }
    return container;
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || "等待输入";
  }

  function stepStatusClass(status) {
    return STEP_STATUS_CLASSES.has(status) ? status : "pending";
  }

  function progressStatusLabel(status) {
    return PROGRESS_STATUS_LABELS[status] || statusLabel(status);
  }

  function stepDetailStatusLabel(status) {
    return STEP_DETAIL_STATUS_LABELS[status] || statusLabel(status);
  }

  function stepStateIcon(status) {
    const icons = {
      completed: "✓",
      error: "!",
      failed: "!",
      waiting_input: "?",
      working: "…",
    };
    return icons[status] || "";
  }

  function stepIsVisible(step) {
    const status = stepStatusClass(normalizeStatus(step && step.status) || "pending");
    return status !== "pending" || (Array.isArray(step && step.events) && step.events.length > 0);
  }

  function stepIsOpen(status) {
    return status === "working" || status === "waiting_input";
  }

  function eventData(event) {
    return event && event.data && typeof event.data === "object" ? event.data : {};
  }

  function firstTextValue(source, keys) {
    if (!source || typeof source !== "object") {
      return "";
    }
    for (const key of keys) {
      const value = source[key];
      if (value === 0 || value) {
        return String(value);
      }
    }
    return "";
  }

  function friendlyFieldLabel(key) {
    return CONCLUSION_FIELD_LABELS[key] || key.replace(/_/g, " ");
  }

  function friendlyValue(value) {
    if (value === true) {
      return "是";
    }
    if (value === false) {
      return "否";
    }
    if (Array.isArray(value)) {
      return value
        .map((item) => {
          if (item && typeof item === "object") {
            return firstTextValue(item, ["title", "name", "label", "summary", "description"]);
          }
          return item === 0 || item ? String(item) : "";
        })
        .filter(Boolean)
        .slice(0, 3)
        .join("、");
    }
    if (value && typeof value === "object") {
      return conclusionText(value);
    }
    return value === 0 || value ? String(value) : "";
  }

  function optionsConclusionText(options) {
    if (!Array.isArray(options) || options.length === 0) {
      return "";
    }
    const names = options
      .map((option) => {
        if (option && typeof option === "object") {
          return firstTextValue(option, ["title", "name", "label", "candidateName"]);
        }
        return option === 0 || option ? String(option) : "";
      })
      .filter(Boolean)
      .slice(0, 2);
    return names.length > 0 ? `已生成 ${options.length} 个方案：${names.join("、")}` : `已生成 ${options.length} 个方案`;
  }

  function conclusionText(conclusion) {
    if (conclusion === 0 || conclusion) {
      if (typeof conclusion !== "object") {
        return String(conclusion);
      }
    } else {
      return "";
    }
    const direct = firstTextValue(conclusion, [
      "summary",
      "title",
      "description",
      "text",
      "result",
      "decision",
      "recommendation",
      "selectedOption",
      "selectedValue",
    ]);
    if (direct) {
      return direct;
    }
    const optionsText = optionsConclusionText(conclusion.options || conclusion.candidates || conclusion.candidateDetails);
    if (optionsText) {
      return optionsText;
    }
    const numericItems = numericConclusionItems(conclusion);
    if (numericItems.length > 0) {
      return `已完成 ${numericItems.length} 个方案评估`;
    }
    return Object.keys(conclusion)
      .filter((key) => !["options", "candidates", "candidateDetails"].includes(key))
      .map((key) => {
        const value = friendlyValue(conclusion[key]);
        return value ? `${friendlyFieldLabel(key)}：${value}` : "";
      })
      .filter(Boolean)
      .join("，");
  }

  function conclusionOptionItems(conclusion) {
    if (!conclusion || typeof conclusion !== "object") {
      return [];
    }
    const options = conclusion.options || conclusion.candidates || conclusion.candidateDetails;
    if (Array.isArray(options)) {
      return options;
    }
    return numericConclusionItems(conclusion);
  }

  function conclusionFieldEntries(conclusion) {
    if (!conclusion || typeof conclusion !== "object" || Array.isArray(conclusion)) {
      return [];
    }
    if (
      firstTextValue(conclusion, [
        "summary",
        "title",
        "description",
        "text",
        "result",
        "decision",
        "recommendation",
        "selectedOption",
        "selectedValue",
      ])
    ) {
      return [];
    }
    if (optionsConclusionText(conclusion.options || conclusion.candidates || conclusion.candidateDetails)) {
      return [];
    }
    return Object.keys(conclusion)
      .filter((key) => !["options", "candidates", "candidateDetails"].includes(key))
      .map((key) => {
        const value = friendlyValue(conclusion[key]);
        return value ? { key, label: friendlyFieldLabel(key), value } : null;
      })
      .filter(Boolean);
  }

  function latestStepCompletion(step) {
    const events = Array.isArray(step && step.events) ? step.events : [];
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      const data = eventData(event);
      const conclusion = data.conclusion || event.conclusion;
      const text = conclusionText(conclusion) || firstTextValue(data, ["summary", "statusMessage", "text", "errorSummary"]);
      if (conclusion || text) {
        return { conclusion, text };
      }
    }
    return { conclusion: null, text: "已完成本步骤。" };
  }

  function completionTextForStep(step) {
    return latestStepCompletion(step).text || "已完成本步骤。";
  }

  function eventText(event) {
    const data = eventData(event);
    const eventType = eventTypeOf(event || {});
    const text =
      firstTextValue(data, ["summary", "text", "statusMessage", "question", "prompt", "candidateName", "errorSummary"]) ||
      conclusionText(data.conclusion || event.conclusion);
    if (text) {
      return text;
    }
    if (eventType === "step_started") {
      return "开始思考";
    }
    if (eventType === "input_required") {
      return "等待您确认或补充信息";
    }
    if (eventType === "candidate_detail_shown") {
      return "生成候选方案详情";
    }
    if (eventType === "permission_requested") {
      return "等待权限确认";
    }
    return eventType || "收到新事件";
  }

  function compactText(value, maxLength = 180) {
    if (value === "" || value === null || value === undefined) {
      return "";
    }
    const text = String(value).replace(/\s+/g, " ").trim();
    return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
  }

  function summarizeValue(value, maxLength = 180) {
    if (value === "" || value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return compactText(value, maxLength);
    }
    try {
      return compactText(JSON.stringify(value), maxLength);
    } catch (_error) {
      return compactText(value, maxLength);
    }
  }

  function toolNameFromEvent(event) {
    const data = eventData(event);
    return data.toolName || data.tool_name || data.name || (data.tool && data.tool.name) || "";
  }

  function objectHasKeys(value) {
    return Boolean(value && typeof value === "object" && Object.keys(value).length > 0);
  }

  function toolSummaryFromEvent(event) {
    const data = eventData(event);
    const result = data.result && typeof data.result === "object" ? data.result : {};
    const directSummary =
      firstTextValue(data, ["safeSummary", "safe_summary", "summary", "text", "statusMessage", "message"]) ||
      firstTextValue(result, ["safeSummary", "safe_summary", "summary", "message", "content", "text"]);
    if (directSummary) {
      return directSummary;
    }
    const stackId = data.stackId || data.stack_id || result.stackId || result.stack_id;
    const stackStatus = data.stackStatus || data.stack_status || result.stackStatus || result.stack_status;
    const resourceId = data.resourceId || data.resource_id || result.resourceId || result.resource_id;
    const resourceName = data.resourceName || data.resource_name || result.resourceName || result.resource_name;
    const status = data.statusMessage || data.statusText || data.status || result.status || "";
    const parts = [stackId, stackStatus, resourceName, resourceId, status]
      .map((part) => compactText(part, 80))
      .filter(Boolean);
    if (parts.length > 0) {
      return parts.join(" · ");
    }
    if (objectHasKeys(result)) {
      return summarizeValue(result, 120);
    }
    return data.action || "";
  }

  function stepEventKind(event) {
    const data = eventData(event);
    const eventType = eventTypeOf(event || {});
    const type = data.type || eventType || "";
    if (type === "tool_result" || eventType === "tool_result") {
      return "tool_result";
    }
    if (type === "tool_use" || eventType === "tool_use" || eventType === "tool_call" || eventType === "tool_started") {
      return "tool_use";
    }
    if (eventType === "input_required") {
      return "input_required";
    }
    if (eventType === "candidate_detail_shown") {
      return "candidate_detail";
    }
    if (eventType === "permission_requested") {
      return "permission";
    }
    if (eventType === "text_delta") {
      return "text_delta";
    }
    return eventType || "event";
  }

  function textDeltaText(event) {
    const data = eventData(event);
    return firstTextValue(data, ["text", "delta", "content", "summary"]);
  }

  function textDeltaMergeKey(event) {
    const candidateIndex = candidateIndexFromSource(event);
    const subStep = candidateSubStepOf(event);
    const subStepId = subStep.id || subStep.stepId || subStep.name || subStep.label || "";
    return `${candidateIndex === null || candidateIndex === undefined ? "" : candidateIndex}|${subStepId}`;
  }

  function compactDisplayEvents(events) {
    return (Array.isArray(events) ? events : []).reduce((result, event) => {
      const kind = stepEventKind(event);
      if (kind !== "text_delta") {
        result.push(clonePlainData(event));
        return result;
      }
      const fragment = textDeltaText(event);
      const previous = result[result.length - 1];
      if (previous && stepEventKind(previous) === "text_delta" && textDeltaMergeKey(previous) === textDeltaMergeKey(event)) {
        previous.data = previous.data && typeof previous.data === "object" ? previous.data : {};
        previous.data.text = `${textDeltaText(previous)}${fragment}`;
      } else {
        const nextEvent = clonePlainData(event);
        nextEvent.data = nextEvent.data && typeof nextEvent.data === "object" ? nextEvent.data : {};
        nextEvent.data.text = fragment;
        result.push(nextEvent);
      }
      return result;
    }, []);
  }

  function stepEventLabel(kind) {
    const labels = {
      candidate_detail: "方案详情",
      input_required: "等待输入",
      permission: "权限确认",
      step_started: "步骤开始",
      text_delta: "思考片段",
      tool_result: "工具结果",
      tool_use: "工具调用",
    };
    return labels[kind] || kind.replace(/_/g, " ");
  }

  function eventTitle(event) {
    const data = eventData(event);
    const kind = stepEventKind(event);
    if (kind === "tool_result" || kind === "tool_use") {
      return toolNameFromEvent(event) || "工具";
    }
    if (kind === "input_required") {
      return firstTextValue(data, ["question", "prompt", "summary"]) || "等待您确认或补充信息";
    }
    if (kind === "candidate_detail") {
      const detail = data.detail && typeof data.detail === "object" ? data.detail : data;
      return firstTextValue(detail, ["candidateName", "name", "title"]) || "生成候选方案详情";
    }
    return eventText(event);
  }

  function eventMetaEntries(event) {
    const data = eventData(event);
    const kind = stepEventKind(event);
    if (kind === "tool_result" || kind === "tool_use") {
      return [
        ["摘要", toolSummaryFromEvent(event)],
        ["地域", data.regionId || data.region_id],
      ];
    }
    if (kind === "input_required") {
      return [["类型", data.kind], ["选项", Array.isArray(data.options) ? `${data.options.length} 个` : ""]];
    }
    if (kind === "permission") {
      return [["工具", data.toolName || data.tool_name], ["原因", data.reason || data.safeSummary]];
    }
    return [];
  }

  function appendKeyValueList(parent, entries, className) {
    const filteredEntries = entries
      .map(([label, value]) => [label, summarizeValue(value)])
      .filter(([_label, value]) => value);
    if (filteredEntries.length === 0) {
      return;
    }
    const list = createElement("dl", className || "key-value-list");
    filteredEntries.forEach(([label, value]) => {
      const row = createElement("div");
      appendChild(row, createElement("dt", "", `${label}：`));
      appendChild(row, createElement("dd", "", value));
      appendChild(list, row);
    });
    appendChild(parent, list);
  }

  function renderStepEvent(event) {
    const kind = stepEventKind(event);
    const item = createElement("li", `step-event-card ${kind}`);
    if (item) {
      item.setAttribute("data-step-event-kind", kind);
    }
    appendChild(item, createElement("span", "step-event-label", stepEventLabel(kind)));
    appendChild(item, createElement("p", "step-event-title", eventTitle(event)));
    appendKeyValueList(item, eventMetaEntries(event), "step-event-meta");
    return item;
  }

  function renderStepProcess(detail, step) {
    const events = compactDisplayEvents(Array.isArray(step && step.events) ? step.events : []);
    if (events.length === 0) {
      return;
    }
    const process = createElement("details", "step-process");
    if (process) {
      process.setAttribute("data-step-process", step.id || "");
    }
    const head = createElement("summary", "step-process-head");
    appendChild(head, createElement("strong", "", "思考过程"));
    appendChild(head, createElement("span", "", `${events.length} 条事件`));
    appendChild(process, head);
    const eventList = createElement("ul", "step-event-list step-process-events");
    events.forEach((event) => {
      const item = renderStepEvent(event);
      if (item) {
        item.setAttribute("data-step-process-event", stepEventKind(event));
      }
      appendChild(eventList, item);
    });
    appendChild(process, eventList);
    appendChild(detail, process);
  }

  function renderStepResult(detail, step) {
    const completion = latestStepCompletion(step);
    const options = conclusionOptionItems(completion.conclusion);
    if (options.length > 0) {
      const list = createElement("div", "step-result-options");
      options.forEach((option, index) => {
        const candidate = candidateFromDisplayItem(option);
        const candidateIndex = candidateIndexOf(candidate, index);
        const item = createElement("article", "step-result-option");
        if (item) {
          item.setAttribute("data-step-result-option", String(candidateIndex));
        }
        appendChild(item, createElement("strong", "", candidate.name || `方案 ${candidateIndex}`));
        if (candidate.summary) {
          appendChild(item, createElement("span", "", candidate.summary));
        }
        if (candidate.template && candidate.template !== candidate.summary && candidate.template !== candidate.name) {
          appendChild(item, createElement("span", "", candidate.template));
        }
        if (candidate.totalMonthlyCost !== "" && candidate.totalMonthlyCost !== null && candidate.totalMonthlyCost !== undefined) {
          appendChild(item, createElement("span", "price", candidate.totalMonthlyCost));
        }
        if (candidate.outputPath) {
          appendChild(item, createElement("span", "template-path", `模板：${candidate.outputPath}`));
        }
        appendChild(list, item);
      });
      appendChild(detail, list);
      return;
    }
    const entries = conclusionFieldEntries(completion.conclusion);
    if (entries.length > 0) {
      const list = createElement("dl", "step-result-list");
      entries.forEach((entry) => {
        const row = createElement("div");
        if (row) {
          row.setAttribute("data-step-result-field", entry.key);
        }
        appendChild(row, createElement("dt", "", `${entry.label}：`));
        appendChild(row, createElement("dd", "", entry.value));
        appendChild(list, row);
      });
      appendChild(detail, list);
      return;
    }
    appendChild(detail, createElement("p", "step-result", completion.text || "已完成本步骤。"));
  }

  function candidateResultSummary(candidate) {
    return (
      (candidate && (candidate.summary || candidate.template || candidate.description || candidate.pros)) ||
      "方案摘要已生成，可在右侧查看完整方案。"
    );
  }

  function isTemplateLikeText(value) {
    const text = String(value || "");
    if (!text) {
      return false;
    }
    return (
      /ROSTemplateFormatVersion|ALIYUN::|Resources:\s|Parameters:\s|Metadata:\s/.test(text) ||
      (text.length > 240 && /Type:\s|Properties:\s|Description:\s/.test(text))
    );
  }

  function candidateResultSummaryDisplay(candidate) {
    const rawSummary = candidateResultSummary(candidate);
    const templateText = candidate && isTemplateLikeText(candidate.template) ? String(candidate.template) : "";
    if (isTemplateLikeText(rawSummary)) {
      return {
        text: "模板内容已生成，悬浮查看完整模板。",
        template: String(rawSummary),
      };
    }
    const compactSummary = compactText(rawSummary, 140);
    return {
      text: compactSummary,
      template: templateText,
      title: compactSummary !== String(rawSummary || "") ? String(rawSummary || "") : "",
    };
  }

  function attachTemplatePopover(host, templateText) {
    if (!host || !templateText) {
      return host;
    }
    addClassName(host, "template-popover-host");
    const popover = createElement("div", "template-popover");
    if (popover) {
      popover.setAttribute("data-template-popover", "true");
      popover.setAttribute("role", "tooltip");
      popover.setAttribute("tabindex", "0");
    }
    appendChild(popover, createElement("div", "template-popover-title", "模板内容"));
    appendChild(popover, createElement("pre", "", templateText));
    appendChild(host, popover);
    return host;
  }

  function renderCandidateProcess(process, candidate, candidateIndex) {
    const events = compactDisplayEvents(Array.isArray(candidate && candidate.subEvents) ? candidate.subEvents : []);
    const renderableEvents = candidateRenderableSubEvents(events);
    if (renderableEvents.length === 0) {
      return;
    }
    const details = createElement("details", "step-candidate-result-process");
    if (details) {
      details.setAttribute("data-step-candidate-result-process", String(candidateIndex));
      details.open = false;
    }
    const head = createElement("summary", "step-process-head");
    appendChild(head, createElement("strong", "", "思考过程"));
    const groups = groupCandidateSubEvents(renderableEvents, { forceComplete: candidateEvaluationIsComplete() });
    appendChild(head, createElement("span", "", `${groups.length} 个子步骤`));
    appendChild(details, head);
    const body = createElement("div", "step-candidate-result-process-body");
    const substeps = createElement("div", "candidate-substeps");
    groups.forEach((group) => {
      appendChild(substeps, renderCandidateSubstepGroup(group));
    });
    appendChild(body, substeps);
    appendChild(details, body);
    appendChild(process, details);
  }

  function renderStepCandidateResults(detail, step) {
    if (!step || step.id !== "evaluate_candidates") {
      return false;
    }
    const state = ensureState();
    const candidates = Array.isArray(state.candidates) ? state.candidates : [];
    if (candidates.length === 0) {
      return false;
    }
    const list = createElement("div", "step-candidate-result-list");
    candidates.forEach((candidate, index) => {
      const candidateIndex = candidateIndexOf(candidate, index);
      const item = createElement("article", "step-candidate-result");
      if (item) {
        item.setAttribute("data-step-candidate-result", String(candidateIndex));
      }
      const summary = candidateResultSummaryDisplay(candidate);
      const head = createElement("div", "step-candidate-result-head");
      appendChild(head, createElement("strong", "", `方案 ${candidateIndex}`));
      appendChild(head, createElement("span", "", candidate.name || `方案 ${candidateIndex}`));
      appendChild(item, head);
      appendChild(item, createElement("span", "step-candidate-result-label", "评估结论"));
      const summaryNode = createElement("p", "step-candidate-result-summary", summary.text);
      if (summaryNode) {
        summaryNode.setAttribute("data-step-candidate-result-summary", String(candidateIndex));
      }
      appendChild(item, summaryNode);
      if (candidate.template && candidate.template !== candidate.summary && !isTemplateLikeText(candidate.template)) {
        appendChild(item, createElement("span", "step-candidate-result-template", candidate.template));
      }
      if (candidate.totalMonthlyCost !== "" && candidate.totalMonthlyCost !== null && candidate.totalMonthlyCost !== undefined) {
        appendChild(item, createElement("span", "step-candidate-result-price", candidate.totalMonthlyCost));
      }
      renderCandidateProcess(item, candidate, candidateIndex);
      attachTemplatePopover(item, summary.template);
      appendChild(list, item);
    });
    appendChild(detail, list);
    return true;
  }

  function candidateProgressText(event) {
    const kind = candidateSubEventKind(event);
    if (kind === "tool_result" || kind === "tool_use") {
      return { label: candidateSubEventLabel(kind), title: eventTitle(event) };
    }
    if (String(kind || "").startsWith("candidate_step")) {
      return { label: candidateSubStepLabel(event), title: eventTitle(event) };
    }
    return { label: stepEventLabel(kind), title: eventTitle(event) };
  }

  function renderStepCandidateProgress(detail) {
    const state = ensureState();
    const candidates = Array.isArray(state.candidates) ? state.candidates : [];
    const rows = candidates
      .map((candidate, index) => {
        const events = compactDisplayEvents(Array.isArray(candidate && candidate.subEvents) ? candidate.subEvents : []);
        return { candidate, candidateIndex: candidateIndexOf(candidate, index), event: events[events.length - 1] };
      })
      .filter((row) => row.event);
    if (rows.length === 0) {
      return false;
    }
    const list = createElement("div", "step-candidate-progress-list");
    rows.forEach((row) => {
      const item = createElement("article", "step-candidate-progress");
      const progress = candidateProgressText(row.event);
      const head = createElement("div", "step-candidate-progress-head");
      if (item) {
        item.setAttribute("data-step-candidate-progress", String(row.candidateIndex));
      }
      if (head) {
        head.setAttribute("data-step-candidate-progress-head", String(row.candidateIndex));
      }
      appendChild(head, createElement("strong", "", `方案 ${row.candidateIndex}`));
      appendChild(head, createElement("span", "", row.candidate.name || `方案 ${row.candidateIndex}`));
      appendChild(item, head);
      appendChild(item, createElement("span", "", progress.label));
      appendChild(item, createElement("p", "", progress.title));
      appendChild(list, item);
    });
    appendChild(detail, list);
    return true;
  }

  function stepCanToggle(status) {
    return status === "completed";
  }

  function stepDetailsExpanded(stepId, status) {
    const state = ensureState();
    return stepCanToggle(status) && Boolean(state.expandedStepDetails && state.expandedStepDetails[stepId]);
  }

  function toggleStepDetails(stepId) {
    const state = ensureState();
    state.expandedStepDetails = state.expandedStepDetails || {};
    state.expandedStepDetails[stepId] = !Boolean(state.expandedStepDetails[stepId]);
    renderAll();
  }

  function renderStepDetails(card, step, status, expanded) {
    if (stepCanToggle(status) && !expanded) {
      return;
    }
    const detail = createElement("div", "step-detail");

    if (stepIsOpen(status)) {
      const badge = createElement("span", "step-status", stepDetailStatusLabel(status));
      appendChild(detail, badge);
      if (status === "waiting_input") {
        const state = ensureState();
        renderPendingInputCard(detail, state);
        renderStepProcess(detail, step);
        appendChild(card, detail);
        return;
      }
      const handledByCandidateSummary = step.id === "evaluate_candidates" && renderStepCandidateProgress(detail);
      if (!handledByCandidateSummary) {
        const events = compactDisplayEvents(Array.isArray(step.events) ? step.events : []);
        const eventList = createElement("ul", "step-event-list");
        if (eventList) {
          eventList.setAttribute("data-step-event-list", step.id || "");
        }
        events.forEach((event) => {
          appendChild(eventList, renderStepEvent(event));
        });
        if (events.length === 0) {
          appendChild(eventList, createElement("li", "step-event-card", STEP_DESCRIPTIONS[step.id] || "正在处理当前步骤"));
        }
        appendChild(detail, eventList);
        scrollElementToBottom(eventList);
      }
    } else if (status === "completed" && expanded) {
      if (!renderStepCandidateResults(detail, step)) {
        renderStepResult(detail, step);
        renderStepProcess(detail, step);
      }
    } else if (status === "failed" || status === "error") {
      const badge = createElement("span", "step-status", stepDetailStatusLabel(status));
      appendChild(detail, badge);
      renderStepResult(detail, step);
      renderStepProcess(detail, step);
    }
    appendChild(card, detail);
  }

  function candidateChoiceText(candidate, fallbackIndex) {
    const candidateIndex = candidateIndexOf(candidate, fallbackIndex);
    const name = candidate && candidate.name ? candidate.name : `方案 ${candidateIndex}`;
    const summary = candidate && candidate.summary ? candidate.summary : "";
    const price = presentValue(candidate && candidate.totalMonthlyCost, "");
    return `${name}${summary}${price}`;
  }

  function pendingInputIsCandidateSelection(pendingInput) {
    if (!pendingInput || typeof pendingInput !== "object") {
      return false;
    }
    const kind = pendingInput.kind || "";
    return kind === "candidate_selection" || kind === "candidate_select";
  }

  function candidatesForPendingSelection(state) {
    const pendingInput = state && state.pendingInput;
    if (!pendingInputIsCandidateSelection(pendingInput)) {
      return [];
    }
    const candidates = Array.isArray(state.candidates) ? state.candidates : [];
    if (candidates.length > 0) {
      return candidates;
    }
    return Array.isArray(pendingInput.options) ? pendingInput.options.map(candidateFromDisplayItem).filter(Boolean) : [];
  }

  function renderCandidateChoiceList(parent, state) {
    const candidates = candidatesForPendingSelection(state);
    if (candidates.length === 0) {
      return false;
    }
    const list = createElement("div", "candidate-choice-list");
    candidates.forEach((candidate, index) => {
      const candidateIndex = candidateIndexOf(candidate, index);
      const isSelected = state.selectedCandidateIndex === candidateIndex;
      const choice = createElement("button", `candidate-choice${isSelected ? " selected" : ""}`);
      if (choice) {
        choice.setAttribute("type", "button");
        choice.setAttribute("data-candidate-choice", String(candidateIndex));
        choice.setAttribute("aria-pressed", isSelected ? "true" : "false");
        choice.addEventListener("click", () => {
          controller.state = selectCandidate(ensureState(), candidateIndex);
          syncComposerWithSelectedCandidate(controller.state);
          renderAll();
        });
      }
      appendChild(choice, createElement("strong", "", candidate.name || `方案 ${candidateIndex}`));
      const summary = candidate.summary || candidate.template || "";
      if (summary) {
        appendChild(choice, createElement("span", "", summary));
      }
      if (candidate.totalMonthlyCost !== "" && candidate.totalMonthlyCost !== null && candidate.totalMonthlyCost !== undefined) {
        appendChild(choice, createElement("span", "price", candidate.totalMonthlyCost));
      }
      appendChild(list, choice);
    });
    appendChild(parent, list);
    return true;
  }

  function pendingInputKindLabel(kind) {
    if (kind === "candidate_selection" || kind === "candidate_select") {
      return "请选择方案";
    }
    if (kind === "ask_user_question") {
      return "需要您确认";
    }
    return "需要您处理";
  }

  function pendingInputPrompt(pendingInput) {
    return pendingInput && (pendingInput.prompt || pendingInput.question || pendingInput.freeTextPrompt || pendingInput.free_text_prompt)
      ? pendingInput.prompt || pendingInput.question || pendingInput.freeTextPrompt || pendingInput.free_text_prompt
      : "请补充信息后继续。";
  }

  function pendingOptionId(option, index) {
    const rawId = option && (option.id ?? option.value ?? option.candidateIndex ?? option.candidate_index ?? index);
    return rawId === null || rawId === undefined ? String(index) : String(rawId);
  }

  function candidateIndexFromPendingOption(option, index) {
    if (option && typeof option === "object") {
      const nestedCandidate = option.candidate && typeof option.candidate === "object" ? option.candidate : {};
      const rawCandidateIndex =
        option.candidateIndex ??
        option.candidate_index ??
        option.optionIndex ??
        option.option_index ??
        nestedCandidate.index ??
        nestedCandidate.candidateIndex ??
        nestedCandidate.candidate_index ??
        null;
      if (rawCandidateIndex !== null && rawCandidateIndex !== undefined && rawCandidateIndex !== "") {
        const numericIndex = Number(rawCandidateIndex);
        return Number.isFinite(numericIndex) ? numericIndex : rawCandidateIndex;
      }
    }
    const optionId = pendingOptionId(option, index);
    const numericOptionId = Number(optionId);
    return Number.isFinite(numericOptionId) ? numericOptionId : null;
  }

  function pendingOptionLabel(option, index) {
    if (!option || typeof option !== "object") {
      return option === 0 || option ? String(option) : `选项 ${index + 1}`;
    }
    return option.label || option.title || option.name || option.candidateName || `选项 ${index + 1}`;
  }

  function pendingOptionDescription(option) {
    if (!option || typeof option !== "object") {
      return "";
    }
    return [option.description || option.summary || "", option.totalMonthlyCost || option.total_monthly_cost || option.price || ""]
      .filter(Boolean)
      .join("");
  }

  function syncComposerWithSelectedCandidate(state) {
    const composer = byId("composer-input");
    if (composer && "value" in composer) {
      composer.value = promptForSelectedCandidate(state || ensureState());
    }
  }

  function handlePendingInputOption(option, index) {
    const state = ensureState();
    const pendingInput = state.pendingInput || {};
    const kind = pendingInput.kind || "";
    const optionId = pendingOptionId(option, index);
    const candidateIndex = candidateIndexFromPendingOption(option, index);
    const composer = byId("composer-input");
    state.selectedPendingInputOptionId = optionId;
    if (kind === "candidate_selection" || kind === "candidate_select") {
      if (candidateIndex !== null && candidateIndex !== undefined) {
        controller.state = selectCandidate(state, candidateIndex);
        controller.state.selectedPendingInputOptionId = optionId;
        syncComposerWithSelectedCandidate(controller.state);
      } else if (composer && "value" in composer) {
        composer.value = optionId || pendingOptionLabel(option, index);
      }
      renderAll();
      return;
    }
    if (candidateIndex !== null && candidateIndex !== undefined) {
      controller.state = selectCandidate(state, candidateIndex);
      controller.state.selectedPendingInputOptionId = optionId;
    }
    if (composer && "value" in composer) {
      composer.value = optionId || pendingOptionLabel(option, index);
    }
    renderAll();
  }

  function renderPendingInputCard(parent, state) {
    const pendingInput = state && state.pendingInput;
    if (!pendingInput) {
      return;
    }
    const kind = pendingInput.kind || "input";
    const isCandidateSelection = pendingInputIsCandidateSelection(pendingInput);
    const card = createElement("section", "pending-input-card");
    if (card) {
      card.setAttribute("data-pending-input-kind", kind);
    }
    appendChild(card, createElement("h2", "", pendingInputKindLabel(kind)));
    appendChild(card, renderMarkdownText(pendingInputPrompt(pendingInput), "pending-input-prompt"));
    const options = Array.isArray(pendingInput.options) ? pendingInput.options : [];
    if (options.length > 0) {
      const optionList = createElement("div", "pending-input-options");
      options.forEach((option, index) => {
        const optionId = pendingOptionId(option, index);
        const candidateIndex = candidateIndexFromPendingOption(option, index);
        const isSelected =
          state.selectedPendingInputOptionId === optionId ||
          (candidateIndex !== null && candidateIndex !== undefined && state.selectedCandidateIndex === candidateIndex);
        const optionButton = createElement("button", `pending-input-option${isSelected ? " selected" : ""}`);
        if (optionButton) {
          optionButton.setAttribute("type", "button");
          optionButton.setAttribute("data-pending-input-option", optionId);
          optionButton.setAttribute("aria-pressed", isSelected ? "true" : "false");
          if (candidateIndex !== null && candidateIndex !== undefined) {
            optionButton.setAttribute("data-candidate-choice", String(candidateIndex));
          }
          optionButton.addEventListener("click", () => handlePendingInputOption(option, index));
        }
        appendChild(optionButton, createElement("strong", "", pendingOptionLabel(option, index)));
        const description = pendingOptionDescription(option);
        if (description) {
          appendChild(optionButton, renderMarkdownText(description, "pending-input-option-description"));
        }
        appendChild(optionList, optionButton);
      });
      appendChild(card, optionList);
    }
    appendChild(parent, card);
  }

  function ensureState() {
    if (!controller.state) {
      const defaults =
        window.SELLING_CONSOLE_DEFAULTS && typeof window.SELLING_CONSOLE_DEFAULTS === "object"
          ? window.SELLING_CONSOLE_DEFAULTS
          : {};
      controller.state = createInitialState(defaults);
    }
    return controller.state;
  }

  function syncConnectionControlsFromState() {
    const state = ensureState();
    const serverInput = byId("server-url");
    const cwdInput = byId("cwd");
    if (serverInput && "value" in serverInput && !serverInput.value && state.serverUrl) {
      serverInput.value = state.serverUrl;
    }
    if (cwdInput && "value" in cwdInput && !cwdInput.value && state.cwd) {
      cwdInput.value = state.cwd;
    }
  }

  function syncStateFromConnectionControls() {
    const state = ensureState();
    const serverInput = byId("server-url");
    const cwdInput = byId("cwd");
    if (serverInput && "value" in serverInput) {
      state.serverUrl = String(serverInput.value || "").trim();
    }
    if (cwdInput && "value" in cwdInput) {
      state.cwd = String(cwdInput.value || "").trim();
    }
    return state;
  }

  function renderStatus() {
    const state = ensureState();
    const statusPill = byId("status-pill");
    if (statusPill) {
      statusPill.textContent = statusLabel(state.pendingInput ? "waiting_input" : state.status);
    }
  }

  function stepModelsForProgress(state, ui, options = {}) {
    const steps = STEP_ORDER.map((stepId, index) => {
      const step = state.steps && state.steps[stepId] ? state.steps[stepId] : createSteps()[stepId];
      const status = stepStatusClass(normalizeStatus(step.status) || "pending");
      return {
        id: stepId,
        index,
        label: step.label || STEP_LABELS[stepId] || stepId,
        status,
      };
    });
    if (options.useConfiguredActiveStep && ui && Number.isInteger(ui.activeStepIndex)) {
      return { steps, activeIndex: ui.activeStepIndex };
    }
    const currentIndex = steps.findIndex((step) => step.status === "working" || step.status === "waiting_input");
    if (currentIndex >= 0) {
      return { steps, activeIndex: currentIndex };
    }
    const lastCompletedIndex = steps.reduce((lastIndex, step, index) => (step.status === "completed" ? index : lastIndex), -1);
    return { steps, activeIndex: Math.max(0, lastCompletedIndex) };
  }

  function progressVisualStatus(step, activeIndex) {
    if (step.status === "failed" || step.status === "error") {
      return "failed";
    }
    if (step.status === "completed" || step.index < activeIndex) {
      return "done";
    }
    if (step.index === activeIndex) {
      return "active";
    }
    return "pending";
  }

  function stepTipText(step, activeIndex) {
    const visualStatus = progressVisualStatus(step, activeIndex);
    if (visualStatus === "done") {
      return `${step.label}：已完成`;
    }
    if (visualStatus === "active") {
      return `${step.label}：当前步骤`;
    }
    if (visualStatus === "failed") {
      return `${step.label}：处理异常`;
    }
    return `${step.label}：等待前序步骤`;
  }

  function applyProgressRoot(progress, variant) {
    const className = variant === "a" ? "composer-progress chevrons" : `composer-progress progress-shell progress-variant-${variant}`;
    progress.className = className;
    if (typeof progress.setAttribute === "function") {
      progress.setAttribute("class", className);
    }
    progress.setAttribute("data-progress-variant", variant);
  }

  function debugDrawerIsOpen() {
    const drawer = byId("debug-drawer");
    return Boolean(drawer && drawer.open);
  }

  function hideComposerProgress(progress, ui) {
    clearElement(progress);
    cancelProgressAnimation();
    progress.hidden = true;
    progress.className = "composer-progress";
    if (typeof progress.setAttribute === "function") {
      progress.setAttribute("class", "composer-progress");
      progress.setAttribute("data-progress-variant", ui.variant);
      progress.setAttribute("data-progress-mode", "pipeline");
      progress.setAttribute("data-progress-visible", "false");
    }
  }

  function renderChevronProgress(progress, models, params) {
    applyProgressRoot(progress, "a");
    progress.setAttribute("style", `--progress-a-sweep-ms: ${params.sweepMs}ms;`);
    models.steps.forEach((step) => {
      const visualStatus = progressVisualStatus(step, models.activeIndex);
      const item = createElement("div", `step ${visualStatus === "done" ? "done" : visualStatus === "active" ? "active" : ""}`);
      if (item) {
        item.setAttribute("data-step-index", String(step.index));
        item.setAttribute("data-progress-step", step.id);
        item.setAttribute("data-status", step.status);
        item.setAttribute("title", stepTipText(step, models.activeIndex));
      }
      appendChild(item, document.createTextNode ? document.createTextNode(step.label) : createElement("span", "", step.label));
      appendChild(item, createElement("span", "tip", stepTipText(step, models.activeIndex)));
      appendChild(progress, item);
    });
  }

  function pathLine(startX, endX, y = 22) {
    return startX === endX ? "" : `M ${startX} ${y} L ${endX} ${y}`;
  }

  function renderSignalProgress(progress, models, params) {
    applyProgressRoot(progress, "b");
    const activeIndex = models.activeIndex;
    const stepPercents = [6, 28, 50, 72, 94];
    const stepXs = [20, 96, 172, 248, 324];
    const railStartX = stepXs[0];
    const railEndX = stepXs[stepXs.length - 1];
    const previousX = activeIndex > 0 ? stepXs[activeIndex - 1] : 0;
    const currentX = stepXs[activeIndex];
    const nextX = activeIndex < stepXs.length - 1 ? stepXs[activeIndex + 1] : 344;
    const shell = createElement("div", "signal-circuit");
    if (shell) {
      shell.setAttribute("data-active-index", String(activeIndex));
      shell.setAttribute("style", `--absorb-duration: ${params.pauseTime}ms;`);
    }
    const svg = createElement("svg", "signal-svg");
    if (svg) {
      svg.setAttribute("viewBox", "0 0 344 44");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      [
        ["signal-rail", pathLine(railStartX, railEndX)],
        ["signal-done", activeIndex > 0 ? pathLine(railStartX, stepXs[activeIndex - 1]) : ""],
        ["signal-active-base signal-active-in", pathLine(previousX, currentX)],
        ["signal-active-base signal-active-out", pathLine(currentX, nextX)],
        ["signal-moving-wave", ""],
      ].forEach(([className, pathValue]) => {
        const path = createElement("path", className);
        if (path) {
          path.setAttribute("d", pathValue);
        }
        appendChild(svg, path);
      });
    }
    appendChild(shell, svg);
    const halo = createElement("span", "signal-absorb-halo");
    if (halo) {
      halo.setAttribute("aria-hidden", "true");
      halo.setAttribute("style", `left:${stepPercents[activeIndex]}%`);
    }
    appendChild(shell, halo);
    models.steps.forEach((step) => {
      const visualStatus = progressVisualStatus(step, activeIndex);
      const nodeClass = [
        "signal-node",
        visualStatus === "active" ? "active" : "",
        visualStatus === "pending" ? "pending" : "",
        step.index === activeIndex + 1 ? "next" : "",
      ].filter(Boolean).join(" ");
      const node = createElement("span", nodeClass);
      if (node) {
        node.setAttribute("data-step-index", String(step.index));
        node.setAttribute("data-progress-step", step.id);
        node.setAttribute("data-status", step.status);
        node.setAttribute("style", `left: ${stepPercents[step.index]}%`);
        node.setAttribute("title", stepTipText(step, activeIndex));
      }
      appendChild(node, createElement("span", "signal-node-charge"));
      appendChild(node, createElement("span", "signal-node-core"));
      appendChild(shell, node);
    });
    const labels = createElement("div", "signal-labels");
    models.steps.forEach((step) => {
      const label = createElement("span", progressVisualStatus(step, activeIndex) === "active" ? "active" : "", step.label);
      if (label) {
        label.setAttribute("data-step-index", String(step.index));
        label.setAttribute("style", `left: ${stepPercents[step.index]}%`);
      }
      appendChild(labels, label);
    });
    appendChild(shell, labels);
    appendChild(progress, shell);
  }

  function renderFusionProgress(progress, models, params) {
    applyProgressRoot(progress, "d");
    const activeIndex = models.activeIndex;
    const shell = createElement("div", "fusion-label");
    if (shell) {
      shell.setAttribute("data-active-index", String(activeIndex));
      shell.setAttribute("style", `--fusion-sweep-duration: ${params.t1}ms;`);
    }
    const steps = createElement("div", "fusion-steps");
    models.steps.forEach((step) => {
      const visualStatus = progressVisualStatus(step, activeIndex);
      const item = createElement("div", `fusion-step ${visualStatus === "done" ? "done" : visualStatus === "active" ? "active" : ""}`);
      if (item) {
        item.setAttribute("data-step-index", String(step.index));
        item.setAttribute("data-progress-step", step.id);
        item.setAttribute("data-status", step.status);
        item.setAttribute("title", stepTipText(step, activeIndex));
      }
      appendChild(item, createElement("span", "label", step.label));
      appendChild(item, createElement("span", "tip", stepTipText(step, activeIndex)));
      appendChild(steps, item);
    });
    appendChild(shell, steps);
    appendChild(progress, shell);
  }

  function renderNormalHandoffMessage(stepList, state) {
    if (!stepList || !state || !state.normalHandoffReady) {
      return false;
    }
    const message = createElement("article", "normal-handoff-message");
    if (message) {
      message.setAttribute("data-normal-handoff-message", "true");
      message.setAttribute("role", "status");
    }
    appendChild(message, createElement("p", "", NORMAL_HANDOFF_TEXT));
    appendChild(stepList, createChatMessage("system", message));
    return true;
  }

  function createChatMessage(role, content) {
    const messageRole = role === "user" ? "user" : "system";
    const message = createElement("div", `chat-message ${messageRole}`);
    if (message) {
      message.setAttribute("data-chat-message", messageRole);
    }
    const avatar = createElement("span", `chat-avatar ${messageRole}`, messageRole === "user" ? "U" : "AI");
    if (avatar) {
      avatar.setAttribute("data-chat-avatar", messageRole);
    }
    const bubble = createElement("div", "chat-bubble");
    appendChild(bubble, content);
    appendChild(message, avatar);
    appendChild(message, bubble);
    return message;
  }

  function createUserMessage(text) {
    return createChatMessage("user", createElement("p", "user-message-text", text));
  }

  function normalProcessIsExpanded(turn) {
    const state = ensureState();
    if (turn && turn.status === "working") {
      return true;
    }
    return Boolean(state.expandedNormalProcesses && turn && state.expandedNormalProcesses[turn.id]);
  }

  function normalProcessEventLabel(kind) {
    return {
      thinking: "思考",
      tool: "工具",
      permission: "权限",
      error: "异常",
    }[kind] || "过程";
  }

  function renderNormalProcess(turn) {
    const events = Array.isArray(turn && turn.events) ? turn.events : [];
    if (!events.length) {
      return null;
    }
    const details = createElement("details", "normal-process");
    if (details) {
      details.setAttribute("data-normal-process", turn.id);
      details.open = normalProcessIsExpanded(turn);
      details.addEventListener("toggle", () => {
        if (turn.status === "working") {
          return;
        }
        const state = ensureState();
        state.expandedNormalProcesses = state.expandedNormalProcesses || {};
        state.expandedNormalProcesses[turn.id] = Boolean(details.open);
      });
    }
    const summary = createElement("summary", "normal-process-summary");
    appendChild(summary, createElement("span", "normal-process-title", "思考过程"));
    appendChild(summary, createElement("span", "normal-process-count", `${events.length} 条`));
    appendChild(details, summary);
    const list = createElement("ul", "normal-process-events");
    events.forEach((event) => {
      const kind = event && event.kind ? String(event.kind) : "event";
      const item = createElement("li", `normal-process-event ${kind}`);
      if (item) {
        item.setAttribute("data-normal-process-event", kind);
      }
      appendChild(item, createElement("span", "normal-process-event-label", event.label || normalProcessEventLabel(kind)));
      appendChild(item, createElement("p", "", event.text || ""));
      appendChild(list, item);
    });
    appendChild(details, list);
    return details;
  }

  function renderNormalTurn(stepList, turn, renderedTurnIds) {
    if (!stepList || !turn || (renderedTurnIds && renderedTurnIds.has(turn.id))) {
      return;
    }
    const content = createElement("article", `normal-turn ${turn.status || "completed"}`);
    if (content) {
      content.setAttribute("data-normal-turn", turn.id);
    }
    appendChild(content, renderNormalProcess(turn));
    const answer = createElement(
      "p",
      "normal-answer",
      turn.answer || (turn.status === "working" ? "正在整理回复..." : "")
    );
    if (answer) {
      answer.setAttribute("data-normal-answer", turn.id);
    }
    appendChild(content, answer);
    appendChild(stepList, createChatMessage("system", content));
    if (renderedTurnIds) {
      renderedTurnIds.add(turn.id);
    }
  }

  function userMessageKey(item, index) {
    return item && item.id ? String(item.id) : `user-message-${index}`;
  }

  function userMessagePlacement(item) {
    const placement = item && item.placement && typeof item.placement === "object" ? item.placement : {};
    if (placement.position === "after_normal_handoff" || placement.after === "normal_handoff") {
      return { position: "after_normal_handoff" };
    }
    if (placement.afterStepId || item.afterStepId) {
      return { position: "after_step", afterStepId: placement.afterStepId || item.afterStepId };
    }
    return { position: "start" };
  }

  function messageBelongsToPosition(item, position, value) {
    const placement = userMessagePlacement(item);
    if (position === "start") {
      return placement.position === "start";
    }
    if (position === "after_normal_handoff") {
      return placement.position === "after_normal_handoff";
    }
    if (position === "after_step") {
      return placement.position === "after_step" && placement.afterStepId === value;
    }
    return false;
  }

  function renderUserMessages(stepList, state, position, value, renderedKeys) {
    const messages = Array.isArray(state && state.userMessages) ? state.userMessages : [];
    messages.forEach((item, index) => {
      const key = userMessageKey(item, index);
      if (renderedKeys && renderedKeys.has(key)) {
        return;
      }
      if (!messageBelongsToPosition(item, position, value)) {
        return;
      }
      const text = item && item.text ? String(item.text) : "";
      if (!text) {
        return;
      }
      appendChild(stepList, createUserMessage(text));
      if (renderedKeys) {
        renderedKeys.add(key);
      }
    });
  }

  function renderNormalHandoffConversation(stepList, state, renderedKeys) {
    const messages = Array.isArray(state && state.userMessages) ? state.userMessages : [];
    const turns = Array.isArray(state && state.normalTurns) ? state.normalTurns : [];
    const renderedTurnIds = new Set();
    messages.forEach((item, index) => {
      const key = userMessageKey(item, index);
      if (renderedKeys && renderedKeys.has(key)) {
        return;
      }
      if (!messageBelongsToPosition(item, "after_normal_handoff", "")) {
        return;
      }
      const text = item && item.text ? String(item.text) : "";
      if (!text) {
        return;
      }
      appendChild(stepList, createUserMessage(text));
      if (renderedKeys) {
        renderedKeys.add(key);
      }
      turns
        .filter((turn) => turn && turn.afterUserMessageId === key)
        .forEach((turn) => renderNormalTurn(stepList, turn, renderedTurnIds));
    });
    turns
      .filter((turn) => turn && !renderedTurnIds.has(turn.id))
      .forEach((turn) => renderNormalTurn(stepList, turn, renderedTurnIds));
  }

  function userMessagePlacementForState(state) {
    if (state && state.normalHandoffReady) {
      return { position: "after_normal_handoff" };
    }
    if (state && pendingInputIsCandidateSelection(state.pendingInput)) {
      return { position: "after_step", afterStepId: "confirm_and_select" };
    }
    const steps = (state && state.steps) || {};
    const activeStepId = STEP_ORDER.find((stepId) => {
      const status = stepStatusClass(normalizeStatus(steps[stepId] && steps[stepId].status));
      return status === "working" || status === "waiting_input";
    });
    if (state && state.pendingInput && activeStepId) {
      return { position: "after_step", afterStepId: activeStepId };
    }
    return { position: "start" };
  }

  function renderSteps() {
    const state = ensureState();
    const stepList = byId("step-list");
    if (!stepList || !canCreateElements()) {
      return;
    }
    clearElement(stepList);
    const renderedUserMessages = new Set();
    renderUserMessages(stepList, state, "start", "", renderedUserMessages);
    STEP_ORDER.forEach((stepId, index) => {
      const step = state.steps && state.steps[stepId] ? state.steps[stepId] : createSteps()[stepId];
      if (!stepIsVisible(step)) {
        return;
      }
      const status = stepStatusClass(normalizeStatus(step.status) || "pending");
      const isCurrent = stepIsOpen(status);
      const isExpanded = stepDetailsExpanded(stepId, status);
      const card = createElement("article", `step-card ${status}${isCurrent ? " current" : ""}`);
      const marker = createElement("span", "step-index");
      const body = createElement("div", "step-card-body");
      const title = createElement("h2", "", step.label || STEP_LABELS[stepId] || stepId);
      if (card) {
        card.setAttribute("data-step-id", stepId);
        card.setAttribute("data-status", status);
        if (isCurrent) {
          card.setAttribute("aria-current", "step");
        }
      }
      const iconText = stepStateIcon(status);
      if (iconText) {
        const icon = createElement("span", `step-state-icon ${status}`, iconText);
        if (icon) {
          icon.setAttribute("data-step-state-icon", status);
        }
        appendChild(marker, icon);
      }
      if (stepCanToggle(status)) {
        const toggle = createElement("button", "step-toggle");
        if (toggle) {
          toggle.setAttribute("type", "button");
          toggle.setAttribute("data-step-toggle", stepId);
          toggle.setAttribute("aria-expanded", isExpanded ? "true" : "false");
          toggle.addEventListener("click", () => toggleStepDetails(stepId));
        }
        appendChild(toggle, title);
        appendChild(toggle, createElement("span", `step-toggle-icon${isExpanded ? " expanded" : ""}`));
        appendChild(body, toggle);
      } else {
        appendChild(body, title);
      }
      appendChild(card, marker);
      appendChild(card, body);
      renderStepDetails(card, step, status, isExpanded);
      appendChild(stepList, createChatMessage("system", card));
      renderUserMessages(stepList, state, "after_step", stepId, renderedUserMessages);
    });
    if (renderNormalHandoffMessage(stepList, state)) {
      renderNormalHandoffConversation(stepList, state, renderedUserMessages);
    }
    renderUserMessages(stepList, state, "after_step", "", renderedUserMessages);
    if (stepList.children && stepList.children.length > 0) {
      scrollElementToBottom(stepList);
    }
  }

  function renderComposerProgress() {
    const state = ensureState();
    const progress = byId("composer-progress");
    if (!progress || !canCreateElements()) {
      return;
    }
    clearElement(progress);
    const ui = mergeProgressUi(state.progressUi);
    state.progressUi = ui;
    const isDebugPreview = debugDrawerIsOpen();
    if (!isDebugPreview && !state.pipelineStarted) {
      hideComposerProgress(progress, ui);
      return;
    }
    progress.hidden = false;
    progress.setAttribute("data-progress-mode", isDebugPreview ? "debug" : "pipeline");
    progress.setAttribute("data-progress-visible", "true");
    const models = stepModelsForProgress(state, ui, { useConfiguredActiveStep: isDebugPreview });
    if (ui.variant === "a") {
      renderChevronProgress(progress, models, ui.a);
    } else if (ui.variant === "d") {
      renderFusionProgress(progress, models, ui.d);
    } else {
      renderSignalProgress(progress, models, ui.b);
    }
    startProgressAnimation();
  }

  function smoothstep(edge0, edge1, value) {
    if (edge0 === edge1) {
      return value < edge0 ? 0 : 1;
    }
    const t = Math.max(0, Math.min(1, (value - edge0) / (edge1 - edge0)));
    return t * t * (3 - 2 * t);
  }

  function cancelProgressAnimation() {
    controller.progressAnimationToken += 1;
    if (controller.progressAnimationFrame !== null && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(controller.progressAnimationFrame);
    }
    if (typeof window !== "undefined" && window.clearTimeout) {
      window.clearTimeout(controller.progressRunTimer);
      window.clearTimeout(controller.progressWaitTimer);
    }
    controller.progressAnimationFrame = null;
    controller.progressRunTimer = 0;
    controller.progressWaitTimer = 0;
  }

  function startFusionProgressAnimation(progress, ui) {
    const label = progress.querySelector ? progress.querySelector(".fusion-label") : null;
    if (!label || typeof requestAnimationFrame !== "function") {
      return;
    }
    const activeIndex = Number(label.getAttribute("data-active-index"));
    const timing = ui.d;

    const percent = (value) => `${Math.max(0, Math.min(100, value)).toFixed(2)}%`;
    const syncBorder = () => {
      const activeStep = label.querySelector(`.fusion-step[data-step-index="${activeIndex}"]`);
      if (!activeStep || !label.getBoundingClientRect || !activeStep.getBoundingClientRect) {
        return;
      }
      const labelRect = label.getBoundingClientRect();
      const activeRect = activeStep.getBoundingClientRect();
      if (!labelRect.width) {
        return;
      }
      const activeStart = ((activeRect.left - labelRect.left) / labelRect.width) * 100;
      const activeEnd = ((activeRect.right - labelRect.left) / labelRect.width) * 100;
      const blueStart = activeIndex === 0 ? 0 : activeStart;
      const greenEnd = activeIndex === 0 ? 0 : activeStart;
      const blueEnd = activeIndex === STEP_ORDER.length - 1 ? 100 : activeEnd;
      label.style.setProperty("--fusion-green-end", percent(greenEnd));
      label.style.setProperty("--fusion-blue-start", percent(blueStart));
      label.style.setProperty("--fusion-blue-end", percent(blueEnd));
      label.style.setProperty("--fusion-sweep-duration", `${timing.t1}ms`);
    };

    const restartSweeps = () => {
      window.clearTimeout(controller.progressRunTimer);
      window.clearTimeout(controller.progressWaitTimer);
      label.classList.remove("sweep-wait");
      label.classList.add("sweep-reset");
      void label.offsetWidth;
      label.classList.remove("sweep-reset");
      controller.progressRunTimer = window.setTimeout(() => {
        label.classList.add("sweep-wait");
        controller.progressWaitTimer = window.setTimeout(restartSweeps, timing.t2);
      }, timing.t1);
    };

    requestAnimationFrame(() => {
      syncBorder();
      restartSweeps();
    });
  }

  function startSignalProgressAnimation(progress, ui) {
    if (typeof requestAnimationFrame !== "function") {
      return;
    }
    const wave = progress.querySelector ? progress.querySelector(".signal-moving-wave") : null;
    const demo = progress.querySelector ? progress.querySelector(".signal-circuit") : null;
    if (!wave || !demo) {
      return;
    }

    const params = ui.b;
    const stepXs = [20, 96, 172, 248, 324];
    const baseY = 22;
    const viewMinX = 0;
    const viewMaxX = 344;
    const virtualPadding = 66;
    const virtualLeftX = stepXs[0] - virtualPadding;
    const virtualRightX = stepXs[stepXs.length - 1] + virtualPadding;
    const nodeClearance = 10;
    const outboundTailClearance = 6;
    let activeIndex = Number(demo.getAttribute("data-active-index"));
    let phase = "inbound";
    let elapsed = 0;
    let pauseLeft = 0;
    let last = typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
    let cycleSalt = 0;
    let absorbTimer = 0;
    const token = controller.progressAnimationToken;

    const clampToView = (x) => Math.max(viewMinX, Math.min(viewMaxX, x));
    const inboundSegment = () => {
      const currentX = stepXs[activeIndex];
      return {
        from: activeIndex === 0 ? virtualLeftX : stepXs[activeIndex - 1] + nodeClearance,
        to: currentX - nodeClearance,
        color: "#1677ff",
        nextPhase: "pause-current",
      };
    };
    const outboundSegment = () => {
      const currentX = stepXs[activeIndex];
      return {
        from: currentX + nodeClearance,
        to: activeIndex === stepXs.length - 1 ? virtualRightX : stepXs[activeIndex + 1] - outboundTailClearance,
        color: "#8f9bae",
        nextPhase: "pause-next",
      };
    };
    const currentSegment = () => (phase === "outbound" || phase === "pause-next" ? outboundSegment() : inboundSegment());
    const segmentMotion = (timeMs) => {
      const x = Math.max(0.04, Math.min(0.48, params.xPercent / 100));
      const y = Math.max(0, Math.min(1, params.yPercent / 100));
      const t1 = Math.max(40, params.t1);
      const t2 = Math.max(80, params.t2);
      if (timeMs < t1) {
        const u = Math.max(0, Math.min(1, timeMs / t1));
        return { anchor: "right", progress: x * u, amplitudeScale: y * smoothstep(0, 1, u), done: false };
      }
      if (timeMs < t1 + t2) {
        const u = Math.max(0, Math.min(1, (timeMs - t1) / t2));
        return { anchor: "right", progress: x + (1 - x) * u, amplitudeScale: y + (1 - y) * Math.sin(Math.PI * u), done: false };
      }
      if (timeMs < t1 * 2 + t2) {
        const u = Math.max(0, Math.min(1, (timeMs - t1 - t2) / t1));
        return { anchor: "left", progress: 1 - x + x * u, amplitudeScale: y * (1 - smoothstep(0, 1, u)), done: false };
      }
      return { anchor: "left", progress: 1, amplitudeScale: 0, done: true };
    };
    const pulseShape = (t) => {
      const micro = 0.1 * Math.sin((t * 2.6 + cycleSalt) * Math.PI);
      const lift = Math.sin(Math.PI * smoothstep(0.16, 0.38, t));
      const drop = Math.sin(Math.PI * smoothstep(0.37, 0.62, t));
      const settle = 0.2 * Math.sin((t - 0.62) * Math.PI * 4.5 + cycleSalt * 0.4);
      return micro + lift - drop * 0.86 + settle * smoothstep(0.58, 0.96, t);
    };
    const movingWavePath = () => {
      if (phase === "pause-current" || phase === "pause-next") {
        return "";
      }
      const segment = currentSegment();
      const segmentLength = segment.to - segment.from;
      const xRatio = Math.max(0.04, Math.min(0.48, params.xPercent / 100));
      const waveLength = segmentLength * xRatio;
      const motion = segmentMotion(elapsed);
      const amplitude = params.maxAmplitude * motion.amplitudeScale;
      if (amplitude < 0.2) {
        return "";
      }
      const right =
        motion.anchor === "left"
          ? segment.from + motion.progress * segmentLength + waveLength
          : segment.from + motion.progress * segmentLength;
      const left = motion.anchor === "left" ? segment.from + motion.progress * segmentLength : right - waveLength;
      const start = Math.max(segment.from, left);
      const end = Math.min(segment.to, right);
      if (end <= segment.from || start >= segment.to || end - start < 1) {
        return "";
      }
      const points = [];
      const samples = 54;
      for (let i = 0; i <= samples; i += 1) {
        const t = i / samples;
        const x = start + t * (end - start);
        const packetT = left < segment.from ? t : (x - left) / waveLength;
        const envelope = smoothstep(0, 0.16, packetT) * (1 - smoothstep(0.84, 1, packetT));
        const y = baseY - pulseShape(packetT) * amplitude * envelope;
        points.push(`${i === 0 ? "M" : "L"} ${clampToView(x).toFixed(2)} ${y.toFixed(2)}`);
      }
      return points.join(" ");
    };
    const render = () => {
      const segment = currentSegment();
      wave.style.stroke = segment.color;
      wave.setAttribute("d", movingWavePath());
    };
    const triggerAbsorbHalo = () => {
      demo.classList.remove("absorbing");
      window.clearTimeout(absorbTimer);
      void demo.offsetWidth;
      demo.classList.add("absorbing");
      absorbTimer = window.setTimeout(() => {
        demo.classList.remove("absorbing");
      }, params.pauseTime);
    };
    const tick = (now) => {
      if (token !== controller.progressAnimationToken) {
        return;
      }
      const dt = Math.min(48, now - last) / 1000;
      last = now;
      if (phase === "pause-current" || phase === "pause-next") {
        pauseLeft -= dt * 1000;
        if (pauseLeft <= 0) {
          if (phase === "pause-current") {
            demo.classList.remove("absorbing");
            window.clearTimeout(absorbTimer);
          }
          phase = phase === "pause-current" ? "outbound" : "inbound";
          elapsed = 0;
          cycleSalt = (cycleSalt + 0.73) % (Math.PI * 2);
        }
        render();
        controller.progressAnimationFrame = requestAnimationFrame(tick);
        return;
      }
      const segment = currentSegment();
      elapsed += dt * 1000;
      if (segmentMotion(elapsed).done) {
        pauseLeft = params.pauseTime;
        phase = segment.nextPhase;
        if (phase === "pause-current") {
          triggerAbsorbHalo();
        }
        elapsed = params.t1 * 2 + params.t2;
      }
      render();
      controller.progressAnimationFrame = requestAnimationFrame(tick);
    };

    requestAnimationFrame((now) => {
      last = now;
      render();
      tick(now);
    });
  }

  function startProgressAnimation() {
    cancelProgressAnimation();
    const progress = byId("composer-progress");
    if (!progress || progress.hidden) {
      return;
    }
    const ui = mergeProgressUi(ensureState().progressUi);
    if (progress.getAttribute("data-progress-variant") === "b") {
      startSignalProgressAnimation(progress, ui);
    }
    if (progress.getAttribute("data-progress-variant") === "d") {
      startFusionProgressAnimation(progress, ui);
    }
  }

  function costItemLabel(item) {
    if (!item || typeof item !== "object") {
      return "";
    }
    const name = item.name || item.resource || item.type || item.product || "费用项";
    const spec = item.spec || item.instanceType || item.instance_type || item.description || "";
    const cost = item.monthly_cost ?? item.monthlyCost ?? item.totalMonthlyCost ?? item.cost ?? "";
    return [name, spec, cost].filter((value) => value !== "" && value !== null && value !== undefined).join(" · ");
  }

  function presentValue(value, fallback) {
    if (value === 0 || value) {
      return String(value);
    }
    return fallback;
  }

  function candidateSubStepOf(event) {
    const data = eventData(event);
    return (
      (event && event.candidateStep && typeof event.candidateStep === "object" ? event.candidateStep : null) ||
      (event && event.candidate_step && typeof event.candidate_step === "object" ? event.candidate_step : null) ||
      (data.candidateStep && typeof data.candidateStep === "object" ? data.candidateStep : null) ||
      (data.candidate_step && typeof data.candidate_step === "object" ? data.candidate_step : null) ||
      {}
    );
  }

  function candidateSubStepLabel(event) {
    const subStep = candidateSubStepOf(event);
    const rawLabel = subStep.label || subStep.name || subStep.title || subStep.id || "";
    const normalizedLabel = String(rawLabel || "").trim();
    if (CANDIDATE_SUBSTEP_LABELS[normalizedLabel]) {
      return CANDIDATE_SUBSTEP_LABELS[normalizedLabel];
    }
    return normalizedLabel || "方案思考";
  }

  function candidateSubEventKind(event) {
    const eventType = eventTypeOf(event || {});
    return String(eventType || "").startsWith("candidate_step") ? eventType : stepEventKind(event);
  }

  function isCandidateLifecycleEvent(event) {
    const eventType = eventTypeOf(event || {});
    return eventType === "candidate_started" || eventType === "candidate_completed" || eventType === "candidate_failed";
  }

  function candidateRenderableSubEvents(events) {
    return (Array.isArray(events) ? events : []).filter((event) => !isCandidateLifecycleEvent(event));
  }

  function candidateSubEventLabel(kind) {
    const labels = {
      candidate_step_completed: "子步骤完成",
      candidate_step_failed: "子步骤失败",
      candidate_step_started: "子步骤开始",
      candidate_started: "方案开始",
      candidate_completed: "方案完成",
      candidate_failed: "方案异常",
      text_delta: "思考片段",
      tool_result: "工具结果",
      tool_use: "工具调用",
    };
    return labels[kind] || stepEventLabel(kind);
  }

  function candidateSubPipelineState(candidate) {
    const events = Array.isArray(candidate && candidate.subEvents) ? candidate.subEvents : [];
    const latest = events[events.length - 1];
    const eventType = eventTypeOf(latest || {});
    const status = normalizeStatus((latest && latest.status) || candidateSubStepOf(latest).status || "");
    if (eventType === "candidate_completed") {
      return "completed";
    }
    if (eventType === "candidate_failed") {
      return "failed";
    }
    if (eventType === "candidate_step_failed" || status === "failed" || status === "error") {
      return "failed";
    }
    return "working";
  }

  function candidateSubPipelineStatus(candidate) {
    const state = candidateSubPipelineState(candidate);
    if (state === "completed") {
      return "思考完成";
    }
    if (state === "failed") {
      return "思考异常";
    }
    return "思考中";
  }

  function candidatePlanStatus(candidate) {
    const events = Array.isArray(candidate && candidate.subEvents) ? candidate.subEvents : [];
    if (events.length === 0) {
      return null;
    }
    const state = candidateSubPipelineState(candidate);
    if (state === "completed") {
      return { state: "completed", label: "已完成" };
    }
    if (state === "failed") {
      return { state: "failed", label: "异常" };
    }
    return { state: "working", label: "生成中" };
  }

  function candidateSubStepId(event, fallbackIndex) {
    const subStep = candidateSubStepOf(event);
    return String(subStep.id || subStep.stepId || subStep.name || subStep.label || `step-${fallbackIndex}`);
  }

  function candidateSubStepStatus(events, forceComplete = false) {
    const latest = events[events.length - 1];
    const eventType = eventTypeOf(latest || {});
    const status = normalizeStatus((latest && latest.status) || candidateSubStepOf(latest).status || "");
    if (eventType === "candidate_step_completed" || status === "completed" || forceComplete) {
      return "completed";
    }
    if (eventType === "candidate_step_failed" || status === "failed" || status === "error") {
      return "failed";
    }
    return "working";
  }

  function groupCandidateSubEvents(events, options = {}) {
    const forceComplete = Boolean(options.forceComplete);
    const groups = [];
    events.forEach((event, index) => {
      const id = candidateSubStepId(event, index);
      let group = groups.find((item) => item.id === id);
      if (!group) {
        group = {
          id,
          label: candidateSubStepLabel(event),
          events: [],
        };
        groups.push(group);
      }
      group.events.push(event);
      group.label = group.label || candidateSubStepLabel(event);
      group.status = candidateSubStepStatus(group.events, forceComplete);
    });
    return groups;
  }

  function candidateEvaluationIsComplete() {
    const state = ensureState();
    const steps = state.steps || {};
    const evaluationStatus = stepStatusClass(normalizeStatus(steps.evaluate_candidates && steps.evaluate_candidates.status));
    const selectionStatus = stepStatusClass(normalizeStatus(steps.confirm_and_select && steps.confirm_and_select.status));
    const deploymentStatus = stepStatusClass(normalizeStatus(steps.deploying && steps.deploying.status));
    return (
      evaluationStatus === "completed" ||
      ["working", "waiting_input", "completed"].includes(selectionStatus) ||
      ["working", "waiting_input", "completed"].includes(deploymentStatus)
    );
  }

  function candidateEvaluationIsWorking() {
    const state = ensureState();
    const steps = state.steps || {};
    return stepStatusClass(normalizeStatus(steps.evaluate_candidates && steps.evaluate_candidates.status)) === "working";
  }

  function scrollElementToBottom(element) {
    if (!element || typeof element.scrollTop === "undefined") {
      return;
    }
    const scroll = () => {
      element.scrollTop = element.scrollHeight || 0;
    };
    scroll();
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(scroll);
    }
  }

  function renderCandidateSubstepGroup(group) {
    const substep = createElement("details", "candidate-substep");
    if (substep) {
      substep.setAttribute("data-candidate-substep", group.id);
      substep.open = group.status !== "completed";
    }
    const substepHead = createElement("summary", "candidate-substep-head");
    appendChild(substepHead, createElement("strong", "", group.label));
    appendChild(substepHead, createElement("span", "", group.status === "completed" ? "完成" : group.status === "failed" ? "异常" : "进行中"));
    appendChild(substep, substepHead);
    const list = createElement("ul", "candidate-subpipeline-events");
    group.events.forEach((event) => {
      const kind = candidateSubEventKind(event);
      const item = createElement("li", `candidate-subpipeline-event ${kind}`);
      if (item) {
        item.setAttribute("data-candidate-subpipeline-event", kind);
      }
      appendChild(item, createElement("span", "candidate-subpipeline-label", candidateSubEventLabel(kind)));
      appendChild(item, createElement("p", "", eventTitle(event)));
      appendChild(list, item);
    });
    appendChild(substep, list);
    return substep;
  }

  function renderCandidateSubPipeline(card, candidate, candidateIndex) {
    const events = compactDisplayEvents(Array.isArray(candidate && candidate.subEvents) ? candidate.subEvents : []);
    const renderableEvents = candidateRenderableSubEvents(events);
    if (renderableEvents.length === 0) {
      return;
    }
    const state = ensureState();
    const pipelineKey = String(candidateIndex);
    const pipelineState = candidateSubPipelineState(candidate);
    const shouldAutoOpen = candidateEvaluationIsWorking() && pipelineState === "working";
    const section = createElement("details", "candidate-subpipeline");
    if (section) {
      section.setAttribute("data-candidate-subpipeline", pipelineKey);
      section.open = shouldAutoOpen || Boolean(state.expandedCandidateSubpipelines && state.expandedCandidateSubpipelines[pipelineKey]);
      section.addEventListener("click", (event) => {
        if (event && typeof event.stopPropagation === "function") {
          event.stopPropagation();
        }
      });
      section.addEventListener("toggle", () => {
        const nextState = ensureState();
        nextState.expandedCandidateSubpipelines = nextState.expandedCandidateSubpipelines || {};
        nextState.expandedCandidateSubpipelines[pipelineKey] = Boolean(section.open);
      });
      section.addEventListener("keydown", (event) => {
        if (event && (event.key === "Enter" || event.key === " ")) {
          event.stopPropagation();
        }
      });
    }
    const head = createElement("summary", "candidate-subpipeline-head");
    if (head) {
      head.setAttribute("data-candidate-subpipeline-toggle", String(candidateIndex));
    }
    appendChild(head, createElement("strong", "", "思考过程"));
    appendChild(head, createElement("span", "candidate-subpipeline-arrow"));
    appendChild(section, head);
    const body = createElement("div", "candidate-subpipeline-body");
    if (body) {
      body.setAttribute("data-candidate-subpipeline-body", pipelineKey);
    }
    const substeps = createElement("div", "candidate-substeps");
    groupCandidateSubEvents(renderableEvents, { forceComplete: pipelineState === "completed" || candidateEvaluationIsComplete() }).forEach((group) => {
      appendChild(substeps, renderCandidateSubstepGroup(group));
    });
    appendChild(body, substeps);
    appendChild(section, body);
    appendChild(card, section);
    if (section && section.open) {
      scrollElementToBottom(body);
    }
  }

  function candidateIndexOf(candidate, fallbackIndex) {
    const rawIndex = candidate && candidate.candidateIndex !== null && candidate.candidateIndex !== undefined
      ? candidate.candidateIndex
      : fallbackIndex;
    const numericIndex = Number(rawIndex);
    return Number.isFinite(numericIndex) ? numericIndex : fallbackIndex;
  }

  function renderPlans() {
    const state = ensureState();
    const plansGrid = byId("plans-grid");
    if (!plansGrid || !canCreateElements()) {
      return;
    }
    clearElement(plansGrid);
    (Array.isArray(state.candidates) ? state.candidates : []).forEach((candidate, index) => {
      const candidateIndex = candidateIndexOf(candidate, index);
      const isSelected = state.selectedCandidateIndex === candidateIndex;
      const isRecommended = isSelected || (state.selectedCandidateIndex === null && index === 0);
      const cardClasses = ["plan-card", isSelected ? "selected" : "", isRecommended ? "recommended" : ""]
        .filter(Boolean)
        .join(" ");
      const card = createElement("article", cardClasses);
      const header = createElement("div", "plan-card-header");
      const tag = createElement("span", `tag${isRecommended ? "" : " muted"}`, isSelected ? "已选" : index === 0 ? "推荐" : "备选");
      const score = createElement("span", "score", `方案 ${candidateIndex}`);
      const planStatus = candidatePlanStatus(candidate);
      const title = createElement("h2", "", candidate.name || `方案 ${candidateIndex}`);
      const summary = createElement("p", "", candidate.summary || "等待方案摘要");
      const price = createElement("div", "price");
      const meta = createElement("dl", "plan-meta");
      const costItems = Array.isArray(candidate.costItems) ? candidate.costItems : [];
      const templateHoverText = isTemplateLikeText(candidate.template) ? String(candidate.template) : "";

      if (card) {
        card.setAttribute("role", "button");
        card.setAttribute("tabindex", "0");
        card.setAttribute("aria-pressed", isSelected ? "true" : "false");
        card.setAttribute("data-candidate-index", String(candidateIndex));
        card.addEventListener("click", () => {
          controller.state = selectCandidate(ensureState(), candidateIndex);
          syncComposerWithSelectedCandidate(controller.state);
          renderAll();
        });
        card.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            controller.state = selectCandidate(ensureState(), candidateIndex);
            syncComposerWithSelectedCandidate(controller.state);
            renderAll();
          }
        });
      }

      appendChild(header, tag);
      const headerMeta = createElement("div", "plan-card-header-meta");
      appendChild(headerMeta, score);
      if (planStatus) {
        const status = createElement("span", `plan-status ${planStatus.state}`, planStatus.label);
        if (status) {
          status.setAttribute("data-candidate-status", planStatus.state);
        }
        appendChild(headerMeta, status);
      }
      appendChild(header, headerMeta);
      appendChild(card, header);
      appendChild(card, title);
      appendChild(card, summary);
      appendChild(price, createElement("span", "price-label", "预估价格"));
      appendChild(price, createElement("strong", "", presentValue(candidate.totalMonthlyCost, "价格待确认")));
      appendChild(card, price);

      costItems.slice(0, 4).forEach((item) => {
        const row = createElement("div");
        const term = createElement("dt", "", item && (item.name || item.resource || item.product) ? item.name || item.resource || item.product : "资源");
        const detail = createElement("dd", "", costItemLabel(item));
        appendChild(row, term);
        appendChild(row, detail);
        appendChild(meta, row);
      });
      appendChild(card, meta);
      renderCandidateSubPipeline(card, candidate, candidateIndex);
      attachTemplatePopover(card, templateHoverText);
      appendChild(plansGrid, card);
    });
  }

  function formatProgressParamValue(definition, value) {
    const numericValue = Number(value);
    const rendered = Number.isFinite(numericValue) && definition.step < 1 ? numericValue.toFixed(1) : String(value);
    return `${rendered}${definition.unit || ""}`;
  }

  function setProgressVariant(variant) {
    const state = ensureState();
    const ui = mergeProgressUi(state.progressUi);
    if (PROGRESS_VARIANT_ORDER.includes(variant)) {
      ui.variant = variant;
    }
    state.progressUi = ui;
    renderAll();
  }

  function setProgressParam(variant, key, value) {
    const state = ensureState();
    const ui = mergeProgressUi(state.progressUi);
    if (!PROGRESS_VARIANT_ORDER.includes(variant) || !Object.prototype.hasOwnProperty.call(ui[variant], key)) {
      return;
    }
    const numericValue = Number(value);
    if (Number.isFinite(numericValue)) {
      ui[variant][key] = numericValue;
      state.progressUi = ui;
      renderAll();
    }
  }

  function setProgressStep(index) {
    const state = ensureState();
    const ui = mergeProgressUi(state.progressUi);
    const numericIndex = Number(index);
    ui.activeStepIndex = Number.isInteger(numericIndex) && numericIndex >= 0 && numericIndex < STEP_ORDER.length ? numericIndex : null;
    state.progressUi = ui;
    renderAll();
  }

  function renderProgressDebugPanel() {
    const panel = byId("progress-debug-panel");
    if (!panel || !canCreateElements()) {
      return;
    }
    const state = ensureState();
    const ui = mergeProgressUi(state.progressUi);
    state.progressUi = ui;
    clearElement(panel);

    const title = createElement("div", "progress-debug-title");
    appendChild(title, createElement("strong", "", "进度条方案"));
    appendChild(title, createElement("span", "", "用于切换视觉方案与调参，不影响 pipeline 状态"));
    appendChild(panel, title);

    const variants = createElement("div", "progress-variant-switch");
    PROGRESS_VARIANT_ORDER.forEach((variant) => {
      const button = createElement("button", ui.variant === variant ? "selected" : "", PROGRESS_VARIANT_LABELS[variant]);
      if (button) {
        button.setAttribute("type", "button");
        button.setAttribute("data-progress-variant-option", variant);
        button.setAttribute("aria-pressed", ui.variant === variant ? "true" : "false");
        button.addEventListener("click", () => setProgressVariant(variant));
      }
      appendChild(variants, button);
    });
    appendChild(panel, variants);

    const activeIndex = stepModelsForProgress(state, ui, { useConfiguredActiveStep: true }).activeIndex;
    const stepControl = createElement("div", "demo-step-control progress-demo-step-control");
    const stepLabel = createElement("label");
    appendChild(stepLabel, createElement("span", "", "演示 Step"));
    appendChild(stepLabel, createElement("output", "", STEP_LABELS[STEP_ORDER[activeIndex]]));
    appendChild(stepControl, stepLabel);
    const stepSwitch = createElement("div", "step-switch");
    if (stepSwitch) {
      stepSwitch.setAttribute("aria-label", "进度条演示当前步骤");
    }
    STEP_ORDER.forEach((stepId, index) => {
      const button = createElement("button", index === activeIndex ? "active" : "", String(index + 1));
      if (button) {
        button.setAttribute("type", "button");
        button.setAttribute("data-progress-step-option", String(index));
        button.setAttribute("aria-pressed", index === activeIndex ? "true" : "false");
        button.setAttribute("title", STEP_LABELS[stepId]);
        button.addEventListener("click", () => setProgressStep(index));
      }
      appendChild(stepSwitch, button);
    });
    appendChild(stepControl, stepSwitch);
    appendChild(panel, stepControl);

    PROGRESS_VARIANT_ORDER.forEach((variant) => {
      const group = createElement("div", "progress-param-grid");
      if (group) {
        group.setAttribute("data-progress-param-group", variant);
        group.hidden = ui.variant !== variant;
      }
      PROGRESS_PARAM_DEFS[variant].forEach((definition) => {
        const value = ui[variant][definition.key];
        const field = createElement("label", "progress-param");
        const head = createElement("span", "progress-param-head");
        appendChild(head, createElement("span", "", definition.label));
        appendChild(head, createElement("output", "", formatProgressParamValue(definition, value)));
        const input = createElement("input");
        if (input) {
          input.setAttribute("type", "range");
          input.setAttribute("min", String(definition.min));
          input.setAttribute("max", String(definition.max));
          input.setAttribute("step", String(definition.step));
          input.setAttribute("data-progress-param", definition.key);
          input.setAttribute("data-progress-param-variant", variant);
          input.value = String(value);
          input.addEventListener("input", () => setProgressParam(variant, definition.key, input.value));
        }
        appendChild(field, head);
        appendChild(field, input);
        appendChild(group, field);
      });
      appendChild(panel, group);
    });
  }

  function renderDebugSessionInfo(state) {
    const container = byId("debug-session-info");
    if (!container) {
      return;
    }
    clearElement(container);
    const fields = [
      ["serverUrl", "Server URL", state.serverUrl || ""],
      ["cwd", "CWD", state.cwd || ""],
      ["contextId", "Context ID", state.contextId || "未获取"],
      ["pipelineTaskId", "Pipeline Task", state.pipelineTaskId || "未获取"],
      ["activeTaskId", "Active Task", state.activeTaskId || "未获取"],
      ["lastSequence", "Last Sequence", String(state.lastSequence || 0)],
      ["status", "Status", state.status || "idle"],
      ["handoff", "Normal Handoff", state.normalHandoffReady ? "是" : "否"],
      ["logs", "Logs", "默认 ~/.iac-code/logs，或 IAC_CODE_CONFIG_DIR/logs"],
    ];
    fields.forEach(([key, label, value]) => {
      const row = createElement("div", "debug-session-field");
      if (row) {
        row.setAttribute("data-debug-session-field", key);
      }
      appendChild(row, createElement("span", "", label));
      appendChild(row, createElement("code", "", value));
      appendChild(container, row);
    });
  }

  function renderDebug() {
    const output = byId("debug-output") || query("#debug-drawer pre");
    const state = ensureState();
    renderDebugSessionInfo(state);
    if (!output) {
      return;
    }
    output.textContent = JSON.stringify(state.diagnostics || {}, null, 2);
  }

  function renderAll() {
    renderStatus();
    renderSteps();
    renderComposerProgress();
    renderPlans();
    renderProgressDebugPanel();
    renderDebug();
  }

  function diagnosticBucket(kind) {
    if (kind === "sse") {
      return "sse";
    }
    if (kind === "snapshot" || kind === "state") {
      return "snapshots";
    }
    return "requests";
  }

  function appendDiagnostic(kind, value) {
    const state = ensureState();
    const diagnostics = state.diagnostics || { requests: [], sse: [], snapshots: [] };
    const bucket = diagnosticBucket(kind);
    const nextValue = clonePlainData({
      at: new Date().toISOString(),
      kind,
      value,
    });
    diagnostics[bucket] = Array.isArray(diagnostics[bucket]) ? diagnostics[bucket] : [];
    diagnostics[bucket].push(nextValue);
    diagnostics[bucket] = diagnostics[bucket].slice(-40);
    state.diagnostics = diagnostics;
    renderDebug();
  }

  function showStatus(message, kind) {
    const alert = byId("status-alert");
    if (!alert) {
      return;
    }
    if (!message) {
      alert.hidden = true;
      alert.textContent = "";
      alert.removeAttribute("data-kind");
      return;
    }
    alert.hidden = false;
    alert.textContent = message;
    alert.setAttribute("data-kind", kind || "info");
  }

  function ensureFetchAvailable() {
    if (typeof fetch === "function") {
      return true;
    }
    appendDiagnostic("error", { error: "fetch is not available" });
    showStatus("当前环境不支持 fetch，无法连接 A2A 服务。", "error");
    return false;
  }

  function queryString(params) {
    if (typeof URLSearchParams === "function") {
      const search = new URLSearchParams();
      Object.keys(params).forEach((key) => {
        search.set(key, params[key] === undefined || params[key] === null ? "" : String(params[key]));
      });
      return search.toString();
    }
    return Object.keys(params)
      .map((key) => `${encodeURIComponent(key)}=${encodeURIComponent(params[key] || "")}`)
      .join("&");
  }

  async function readJsonResponse(response) {
    const text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (error) {
      return { ok: false, error: String(error), text };
    }
  }

  function errorMessage(error) {
    return error && error.message ? error.message : String(error);
  }

  function activeTaskIdFromPayload(payload) {
    const envelope = extractPipelineEnvelope(payload);
    const envelopeTaskId = taskIdOf(envelope || {});
    if (envelopeTaskId) {
      return envelopeTaskId;
    }
    if (payload && payload.result && typeof payload.result === "object") {
      return payload.result.taskId || payload.result.task_id || payload.result.id || "";
    }
    if (payload && payload.task && typeof payload.task === "object") {
      return payload.task.taskId || payload.task.task_id || payload.task.id || "";
    }
    return taskIdOf(payload || {}) || "";
  }

  function isWaitingForInputPayload(payload, state) {
    const envelope = extractPipelineEnvelope(payload);
    return (
      Boolean(state && state.pendingInput) ||
      (state && state.status === "waiting_input") ||
      eventTypeOf(envelope || {}) === "input_required" ||
      normalizeStatus((envelope && envelope.status) || "") === "waiting_input"
    );
  }

  function waitForNextPaint() {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => resolve());
        return;
      }
      if (typeof window !== "undefined" && typeof window.setTimeout === "function") {
        window.setTimeout(resolve, 16);
        return;
      }
      if (typeof setTimeout === "function") {
        setTimeout(resolve, 0);
        return;
      }
      resolve();
    });
  }

  function reduceControllerPayload(payload) {
    const currentState = ensureState();
    const nextState = reducePipelinePayload(currentState, payload);
    const activeTaskId = activeTaskIdFromPayload(payload);
    if (!nextState.normalHandoffReady && activeTaskId) {
      nextState.activeTaskId = activeTaskId;
    }
    controller.state = nextState;
    renderAll();
    return nextState;
  }

  function handleSseBlock(block) {
    const dataLines = String(block || "")
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart());
    if (dataLines.length === 0) {
      return false;
    }
    const data = dataLines.join("\n").trim();
    if (!data || data === "[DONE]") {
      return false;
    }
    let payload;
    try {
      payload = JSON.parse(data);
    } catch (error) {
      appendDiagnostic("sse", { error: String(error), data });
      showStatus("收到无法解析的 SSE 数据，详情见调试信息。", "error");
      return false;
    }
    appendDiagnostic("sse", payload);
    if (payload && payload.ok === false) {
      throw new Error(payload.error || payload.message || "SSE stream reported an error");
    }
    const nextState = reduceControllerPayload(payload);
    return isWaitingForInputPayload(payload, nextState);
  }

  async function consumeSseResponse(response) {
    if (!response.ok) {
      const errorText = typeof response.text === "function" ? await response.text() : "";
      throw new Error(`HTTP ${response.status}: ${errorText}`);
    }
    if (!response.body || typeof response.body.getReader !== "function") {
      const text = typeof response.text === "function" ? await response.text() : "";
      const blocks = text
        .replace(/\r\n/g, "\n")
        .split("\n\n")
        .filter((block) => block.trim());
      for (const block of blocks) {
        const shouldStop = handleSseBlock(block);
        await waitForNextPaint();
        if (shouldStop) {
          break;
        }
      }
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let shouldStop = false;
    while (!shouldStop) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        shouldStop = handleSseBlock(block);
        await waitForNextPaint();
        if (shouldStop) {
          break;
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (!shouldStop && buffer.trim()) {
      handleSseBlock(buffer);
      await waitForNextPaint();
    }
    if (shouldStop && typeof reader.cancel === "function") {
      await reader.cancel();
    }
  }

  async function sendComposerMessage() {
    if (!ensureFetchAvailable()) {
      return;
    }
    const state = syncStateFromConnectionControls();
    const composer = byId("composer-input");
    const typedPrompt = composer && "value" in composer ? String(composer.value || "").trim() : "";
    const prompt = typedPrompt || promptForSelectedCandidate(state);
    if (!prompt) {
      showStatus("请输入需求，或先选择一个方案。", "error");
      return;
    }
    state.userMessages = Array.isArray(state.userMessages) ? state.userMessages : [];
    const userMessageId = `user-${Date.now()}-${state.userMessages.length}`;
    state.userMessages.push({
      id: userMessageId,
      text: prompt,
      placement: userMessagePlacementForState(state),
    });
    if (state.normalHandoffReady) {
      state.pendingNormalUserMessageId = userMessageId;
    }
    if (composer && "value" in composer && typedPrompt) {
      composer.value = "";
    }
    renderAll();
    const payload = buildStreamPayload(state, prompt);
    appendDiagnostic("request", { method: "POST", path: "/api/message/stream", payload });
    showStatus("正在发送消息并接收 pipeline 事件...", "info");
    try {
      const response = await fetch("/api/message/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await consumeSseResponse(response);
      showStatus(ensureState().pendingInput ? "请选择或补充输入后继续。" : "消息已发送，状态已更新。", "info");
    } catch (error) {
      const message = errorMessage(error);
      appendDiagnostic("error", { action: "send", error: message });
      showStatus(`消息发送失败：${message}`, "error");
    }
  }

  async function healthCheck() {
    if (!ensureFetchAvailable()) {
      return;
    }
    const state = syncStateFromConnectionControls();
    const path = `/api/health?${queryString({ serverUrl: state.serverUrl })}`;
    appendDiagnostic("request", { method: "GET", path });
    try {
      const response = await fetch(path);
      const body = await readJsonResponse(response);
      appendDiagnostic("request", { method: "GET", path, status: response.status, body });
      showStatus(response.ok ? "连接检查完成。" : `连接检查失败：HTTP ${response.status}`, response.ok ? "info" : "error");
    } catch (error) {
      const message = errorMessage(error);
      appendDiagnostic("error", { action: "health", error: message });
      showStatus(`连接检查失败：${message}`, "error");
    }
  }

  async function fetchState() {
    if (!ensureFetchAvailable()) {
      return;
    }
    const state = syncStateFromConnectionControls();
    const taskId = state.activeTaskId || state.pipelineTaskId || "";
    const path = `/api/pipeline/state?${queryString({
      serverUrl: state.serverUrl,
      contextId: state.contextId || "",
      taskId,
      afterSequence: state.lastSequence || 0,
    })}`;
    appendDiagnostic("request", { method: "GET", path });
    try {
      const response = await fetch(path);
      const body = await readJsonResponse(response);
      appendDiagnostic("state", { status: response.status, body });
      if (body) {
        reduceControllerPayload(body);
      }
      showStatus(response.ok ? "状态已同步。" : `同步状态失败：HTTP ${response.status}`, response.ok ? "info" : "error");
    } catch (error) {
      const message = errorMessage(error);
      appendDiagnostic("error", { action: "fetchState", error: message });
      showStatus(`同步状态失败：${message}`, "error");
    }
  }

  async function cancelTask() {
    if (!ensureFetchAvailable()) {
      return;
    }
    const state = syncStateFromConnectionControls();
    const payload = {
      serverUrl: state.serverUrl || "",
      contextId: state.contextId || "",
      taskId: state.activeTaskId || state.pipelineTaskId || "",
    };
    appendDiagnostic("request", { method: "POST", path: "/api/task/cancel", payload });
    try {
      const response = await fetch("/api/task/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await readJsonResponse(response);
      appendDiagnostic("request", { method: "POST", path: "/api/task/cancel", status: response.status, body });
      showStatus(response.ok ? "取消请求已发送。" : `取消任务失败：HTTP ${response.status}`, response.ok ? "info" : "error");
    } catch (error) {
      const message = errorMessage(error);
      appendDiagnostic("error", { action: "cancel", error: message });
      showStatus(`取消任务失败：${message}`, "error");
    }
  }

  function bindEvents() {
    if (controller.bound) {
      return;
    }
    const serverInput = byId("server-url");
    const cwdInput = byId("cwd");
    const sendButton = byId("send-button");
    const composer = byId("composer-input");
    const healthButton = byId("health-button");
    const fetchStateButton = byId("fetch-state-button");
    const cancelButton = byId("cancel-button");
    const debugDrawer = byId("debug-drawer");
    const addListener = (element, eventName, handler) => {
      if (element && typeof element.addEventListener === "function") {
        element.addEventListener(eventName, handler);
      }
    };

    addListener(serverInput, "input", syncStateFromConnectionControls);
    addListener(cwdInput, "input", syncStateFromConnectionControls);
    addListener(sendButton, "click", sendComposerMessage);
    addListener(healthButton, "click", healthCheck);
    addListener(fetchStateButton, "click", fetchState);
    addListener(cancelButton, "click", cancelTask);
    addListener(debugDrawer, "toggle", renderAll);
    addListener(composer, "keydown", (event) => {
      if ((event.key === "Enter" && !event.shiftKey) || (event.key === "Enter" && (event.metaKey || event.ctrlKey))) {
        event.preventDefault();
        sendComposerMessage();
      }
    });
    controller.bound = Boolean(
      serverInput || cwdInput || sendButton || composer || healthButton || fetchStateButton || cancelButton || debugDrawer
    );
  }

  function loadDemoCandidates() {
    let state = ensureState();
    state = upsertCandidate(state, {
      name: "ECS 经典网络方案",
      candidateIndex: 0,
      summary: "使用 VPC、ECS 与弹性公网 IP 搭建轻量 Web 服务，保留后续扩容空间。",
      totalMonthlyCost: "¥33.89/月",
      costItems: [
        { name: "ECS", spec: "1vCPU/1GiB", monthly_cost: "¥33.89/月" },
        { name: "EIP", spec: "按量公网带宽", monthly_cost: "按实际流量" },
      ],
    });
    state = upsertCandidate(state, {
      name: "轻量应用服务器一体化方案",
      candidateIndex: 1,
      summary: "面向演示、测试与低流量站点，预置应用环境并降低运维复杂度。",
      totalMonthlyCost: "¥0/月",
      costItems: [
        { name: "轻量应用服务器", spec: "试用规格", monthly_cost: "¥0/月" },
        { name: "基础监控", spec: "默认启用", monthly_cost: "¥0/月" },
      ],
    });
    state.steps.intent_parsing.status = "completed";
    state.steps.architecture_planning.status = "completed";
    state.steps.evaluate_candidates.status = "completed";
    state.steps.confirm_and_select.status = "waiting_input";
    state.status = "waiting_input";
    state.pipelineStarted = true;
    state.pendingInput = {
      kind: "candidate_selection",
      prompt: "请选择推荐方案",
      options: [
        { id: "0", label: "ECS 经典网络方案" },
        { id: "1", label: "轻量应用服务器一体化方案" },
      ],
    };
    controller.state = state;
    renderAll();
    return state;
  }

  function init() {
    ensureState();
    syncConnectionControlsFromState();
    syncStateFromConnectionControls();
    bindEvents();
    renderAll();
    return controller.state;
  }

  window.SellingConsoleController = {
    init,
    renderSteps,
    renderPlans,
    sendComposerMessage,
    healthCheck,
    fetchState,
    cancelTask,
    appendDiagnostic,
    renderDebug,
  };
  window.SellingConsoleDebug = {
    loadDemoCandidates,
    state: () => ensureState(),
    render: renderAll,
  };

  if (hasDocument()) {
    if (document.readyState === "loading" && typeof document.addEventListener === "function") {
      document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
      init();
    }
  }
})();
