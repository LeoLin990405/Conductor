import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Ticket } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Loader } from '@/components/ui/loader';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';
import { useUser } from '@/hooks/useUser';
import { getCurrentUser } from '@/lib/dashboardApi/api';

/**
 * Platform invitation-code redemption, shown beside provider choice while the
 * account has no platform access yet. The provider step is the setup flow's
 * first page now, so the invitation entry that used to live on the method page
 * lives here.
 */
export default function SetupInvitation() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, isLoading } = useUser();
  const [localRedeemed, setLocalRedeemed] = useState(false);
  const hasPlatformAccess = (user?.access_tier ?? -1) >= 0 || localRedeemed;
  const [showInvitation, setShowInvitation] = useState(false);
  const [invitationCode, setInvitationCode] = useState('');
  const [invitationError, setInvitationError] = useState<string | null>(null);
  const [redeeming, setRedeeming] = useState(false);

  const handleRedeem = useCallback(async () => {
    if (!invitationCode.trim()) {
      setInvitationError(t('setup.invitationErrorEmpty'));
      return;
    }

    setRedeeming(true);
    setInvitationError(null);
    try {
      await api.post('/api/auth/invitations/redeem', { code: invitationCode.trim() });
      setLocalRedeemed(true);
      await getCurrentUser({ refresh_tier: true }).catch(() => {});
      await queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
      navigate('/setup/defaults');
    } catch (e: unknown) {
      const err = e as {
        response?: { status?: number; data?: { detail?: string | { message?: string; type?: string } } };
        message?: string;
      };
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail;

      if (status === 404) {
        setInvitationError(t('setup.invitationErrorInvalid'));
      } else if (status === 410) {
        setInvitationError(t('setup.invitationErrorExpired'));
      } else if (status === 409) {
        setLocalRedeemed(true);
        await getCurrentUser({ refresh_tier: true }).catch(() => {});
        await queryClient.invalidateQueries({ queryKey: queryKeys.user.me() });
        await queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() });
        navigate('/setup/defaults');
      } else if (typeof detail === 'string') {
        setInvitationError(detail);
      } else if (detail && typeof detail === 'object' && 'message' in detail) {
        setInvitationError(detail.message || t('setup.invitationErrorGeneric'));
      } else {
        setInvitationError(t('setup.invitationErrorGeneric'));
      }
    } finally {
      setRedeeming(false);
    }
  }, [invitationCode, queryClient, navigate, t]);

  if (!isLoading && hasPlatformAccess) return null;

  return (
    <div className="mt-6">
      {!showInvitation ? (
        <button
          type="button"
          onClick={() => setShowInvitation(true)}
          className="inline-flex items-center gap-1.5 text-sm font-medium text-(--color-text-secondary) transition-colors hover:text-(--color-text-primary)"
        >
          <Ticket className="h-4 w-4" />
          {t('setup.invitationCodeLink')}
        </button>
      ) : (
        <div className="flex flex-col gap-3 rounded-lg border border-(--color-border-muted) bg-(--color-bg-card) p-4">
          <label className="text-sm font-medium text-(--color-text-primary)">
            {t('setup.invitationCodeLabel')}
          </label>
          <div className="flex gap-2">
            <Input
              value={invitationCode}
              onChange={(e) => {
                setInvitationCode(e.target.value);
                setInvitationError(null);
              }}
              placeholder={t('setup.invitationCodePlaceholder')}
              className="flex-1"
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              variant="default"
              disabled={redeeming || !invitationCode.trim()}
              onClick={handleRedeem}
              className="shrink-0"
            >
              {redeeming ? (
                <>
                  <span aria-hidden="true" className="mr-1.5 shrink-0">
                    <Loader size={16} className="text-current" />
                  </span>
                  {t('setup.redeeming')}
                </>
              ) : (
                t('setup.redeem')
              )}
            </Button>
          </div>
          {invitationError && (
            <p className="text-xs text-(--color-loss)">{invitationError}</p>
          )}
          <button
            type="button"
            onClick={() => {
              setShowInvitation(false);
              setInvitationError(null);
            }}
            className="self-start text-xs text-(--color-text-tertiary)"
          >
            {t('commonUi.cancel')}
          </button>
        </div>
      )}
    </div>
  );
}
