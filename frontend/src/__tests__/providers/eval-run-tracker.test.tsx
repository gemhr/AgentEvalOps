import { fireEvent, render, waitFor } from "@testing-library/react";
import {
  EvalRunTrackerProvider,
  useEvalRunTracker,
} from "@/components/providers/EvalRunTrackerProvider";
import { EvaluationStatus } from "@/lib/api/enums";

const mockToast = jest.fn();
const mockInvalidateQueries = jest.fn();
const mockResults = [
  {
    data: {
      id: "run-1",
      project_id: "project-1",
      name: "Nightly evaluation",
      status: EvaluationStatus.OUTCOME_UNKNOWN,
      metric_names: [],
      total_targets: 1,
      evaluated_count: 0,
      failed_count: 0,
      created_at: null,
      completed_at: null,
      target_type: "TRACE",
      filters: {},
      sampling_rate: 1,
      model: null,
      monitor_id: null,
      error_message: null,
    },
    error: null,
  },
];

jest.mock("@tanstack/react-query", () => ({
  useQueries: () => mockResults,
  useQueryClient: () => ({ invalidateQueries: mockInvalidateQueries }),
}));

jest.mock("@/lib/api/evaluations", () => ({
  getTraceRun: jest.fn(),
  getSessionRun: jest.fn(),
}));

jest.mock("@/hooks/useNavigation", () => ({ useProjectId: () => "project-1" }));

jest.mock("@/components/providers/ToastProvider", () => ({
  CORNER_STACK_SLOT_ID: "toast-slot",
  useToast: () => ({ toast: mockToast }),
}));

function RegisterRun() {
  const tracker = useEvalRunTracker();
  return (
    <button
      onClick={() =>
        tracker?.register({
          runId: "run-1",
          mode: "trace",
          targetIds: ["trace-1"],
        })
      }
    >
      Register run
    </button>
  );
}

describe("EvalRunTrackerProvider", () => {
  it("treats OUTCOME_UNKNOWN as terminal and reports the uncertain result", async () => {
    const { getByRole } = render(
      <EvalRunTrackerProvider>
        <RegisterRun />
      </EvalRunTrackerProvider>,
    );

    fireEvent.click(getByRole("button", { name: "Register run" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: "Evaluation outcome unknown",
        description:
          "'Nightly evaluation' · The evaluation outcome could not be confirmed.",
        variant: "error",
      });
    });
    expect(mockInvalidateQueries).toHaveBeenCalled();
  });
});
