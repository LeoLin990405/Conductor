import { AnimatePresence } from '@/lib/framer';
import { useConnectQuestion } from '../connectQuestion';
import { LeaveForSignIn } from './LeaveForSignIn';

/**
 * The one question a connect raises before any OAuth provider's page: the app
 * is about to leave for the vendor, and what does not happen until the user is
 * back. The lifecycle holds the request until it is answered.
 */
export function ConnectConsent() {
  const question = useConnectQuestion();
  const request = question?.request;
  return (
    <AnimatePresence>
      {question && request && (
        <LeaveForSignIn
          key={`leave:${request.name}`}
          request={request}
          pending={question.starting}
          onConfirm={() => question.answer()}
          onCancel={question.cancel}
        />
      )}
    </AnimatePresence>
  );
}

export default ConnectConsent;
